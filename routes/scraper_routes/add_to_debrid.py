"""Add a selected torrent/magnet to the debrid provider."""

from flask import jsonify, request
import logging
from ..models import user_required, scraper_permission_required
from utilities.settings import get_setting
from utilities.local_library_scan import extract_resolution_from_filename
from queues.media_matcher import MediaMatcher
import requests
import tempfile
import os
import bencodepy
import hashlib
from datetime import timezone
from database.torrent_tracking import record_torrent_addition, get_torrent_history, update_torrent_tracking
from flask_login import current_user
from ..utils import is_user_system_enabled
import re
import json
import iso8601
from utilities.reverse_parser import parse_filename_for_version
from ._common import ContentProcessor, _record_manual_grab, obfuscate_magnet_link, scraper_bp
from .usenet_add import _add_nzb_pack_to_usenet, _add_nzb_to_usenet, _build_debrid_title

@scraper_bp.route('/add_to_debrid', methods=['POST'])
@user_required
@scraper_permission_required
def add_torrent_to_debrid():
    from metadata.metadata import get_metadata, _get_local_timezone, get_release_date
    try:
        magnet_link = request.form.get('magnet_link')
        title = request.form.get('title')
        year = request.form.get('year')
        media_type = request.form.get('media_type')
        season = request.form.get('season')
        episode = request.form.get('episode')
        version_from_form = request.form.get('version') # Renamed to avoid conflict
        tmdb_id = request.form.get('tmdb_id')
        original_scraped_torrent_title = request.form.get('original_scraped_torrent_title')
        source_context = request.form.get('source_context')  # e.g. 'recently_aired'

        # Only configured versions (an upgrade's trailing '*' allowed), "No Version"
        # (reverse-parsed below) or none. An unknown name made an item that no
        # version settings can scrape or upgrade, as /content/request did.
        if version_from_form and version_from_form != "No Version":
            configured_versions = get_setting('Scraping', 'versions', {}) or {}
            if version_from_form.rstrip('*') not in configured_versions:
                return jsonify({'error': f"Unknown version: {version_from_form}"}), 400
        # Get file management mode to check if we should process folder selection
        file_management_mode = get_setting('File Management', 'file_collection_management', 'Plex')

        # Only get folder selection data if in symlink mode
        selected_folder = None
        selected_folder_is_custom = False
        if file_management_mode == 'Symlinked/Local':
            selected_folder = request.form.get('selected_folder')  # Get user-selected folder for symlink mode
            selected_folder_is_custom = request.form.get('selected_folder_is_custom') == 'true'  # Check if it's a custom folder

        # Get tags selection — Plex mode only, NZB naming embeds tags in job title
        selected_tags = None
        if file_management_mode == 'Plex':
            _tags_raw = request.form.get('selected_tags', '').strip()
            if _tags_raw:
                selected_tags = _tags_raw

        # Get current_score from form data
        current_score_str = request.form.get('current_score', '0') # Default to '0'
        try:
            current_score = float(current_score_str)
        except (ValueError, TypeError):
            logging.warning(f"Invalid current_score value received: '{current_score_str}'. Defaulting to 0.")
            current_score = 0.0

        logging.info(f"Adding {title} ({year}) to debrid provider")

        # Only log folder selection details if in symlink mode
        if file_management_mode == 'Symlinked/Local':
            logging.info(f"========== FOLDER SELECTION DEBUG ==========")
            logging.info(f"selected_folder: {selected_folder}")
            logging.info(f"selected_folder_is_custom: {selected_folder_is_custom}")
            logging.info(f"============================================")
            if selected_folder:
                folder_type = "custom" if selected_folder_is_custom else "standard"
                logging.info(f"✅ User selected folder: {selected_folder} (type: {folder_type})")
            else:
                logging.info(f"⚠️ No folder selected - will use genre-based auto-detection")

        # Determine the final version for the item
        final_version_for_item = version_from_form
        if version_from_form == "No Version":
            if original_scraped_torrent_title:
                logging.info(f"Attempting to reverse parse version for torrent '{original_scraped_torrent_title}' as 'No Version' was selected.")
                final_version_for_item = parse_filename_for_version(original_scraped_torrent_title)
            else:
                logging.warning("'No Version' selected, but original_scraped_torrent_title is missing. Attempting reverse parse with empty string.")
                final_version_for_item = parse_filename_for_version("") # reverse_parser will use its default
            logging.info(f"Version to be assigned: {final_version_for_item}")

        # Get metadata to determine genres
        metadata = get_metadata(tmdb_id=tmdb_id, item_media_type=media_type) if tmdb_id else {}
        genres = metadata.get('genres', [])
        if isinstance(genres, str):
            # If genres come as comma-separated string, convert to list
            genres = [g.strip() for g in genres.split(',')]
        elif not isinstance(genres, list):
            genres = []
        logging.info(f"Genres from metadata: {genres}")

        # Convert season and episode to integers or None (keep 0 for Specials on TV; movies have no season/episode)
        def _parse_opt_int(val):
            if val is None:
                return None
            s = str(val).strip()
            if not s or s.lower() in ('null', 'none'):
                return None
            try:
                return int(s)
            except (ValueError, TypeError):
                return None

        # Only a movie has no season: an empty/missing media_type keeps the numbers as before
        if media_type != 'movie':
            season_number = _parse_opt_int(season)
            episode_number = _parse_opt_int(episode)
        else:
            season_number = None
            episode_number = None
            
        # NZB / Usenet path — route to cli_mount instead of debrid
        protocol = request.form.get('protocol', '').lower()
        nzb_url = request.form.get('nzb_url', '')
        episode_nzb_urls_raw = request.form.get('episode_nzb_urls', '')
        fallback_nzb_urls_raw = request.form.get('fallback_nzb_urls', '')
        episode_filenames_raw = request.form.get('episode_filenames', '')
        pack_waterfall_level_raw = request.form.get('pack_waterfall_level', '')
        if protocol == 'nzb' or (nzb_url and not magnet_link) or episode_nzb_urls_raw:
            # Virtual season pack — per-episode NZB URLs
            if episode_nzb_urls_raw:
                try:
                    episode_nzb_urls = {int(k): v for k, v in json.loads(episode_nzb_urls_raw).items()}
                    fallback_nzb_urls = {int(k): v for k, v in json.loads(fallback_nzb_urls_raw).items()} if fallback_nzb_urls_raw else {}
                    episode_filenames_ui = {int(k): v for k, v in json.loads(episode_filenames_raw).items()} if episode_filenames_raw else {}
                except Exception:
                    episode_nzb_urls = {}
                    fallback_nzb_urls = {}
                    episode_filenames_ui = {}
                if episode_nzb_urls:
                    try:
                        pack_waterfall_level = int(pack_waterfall_level_raw) if pack_waterfall_level_raw else None
                    except (ValueError, TypeError):
                        pack_waterfall_level = None
                    return _add_nzb_pack_to_usenet(
                        episode_nzb_urls=episode_nzb_urls,
                        fallback_nzb_urls=fallback_nzb_urls,
                        title=title, year=year, media_type=media_type,
                        season=season_number,
                        version=final_version_for_item, tmdb_id=tmdb_id,
                        original_scraped_torrent_title=original_scraped_torrent_title,
                        episode_filenames=episode_filenames_ui,
                        genres=genres, current_score=current_score,
                        selected_folder=selected_folder,
                        selected_folder_is_custom=selected_folder_is_custom,
                        selected_tags=selected_tags,
                        waterfall_level=pack_waterfall_level,
                    )
            return _add_nzb_to_usenet(
                nzb_url=nzb_url or magnet_link,
                title=title, year=year, media_type=media_type,
                season=season_number, episode=episode_number,
                version=final_version_for_item, tmdb_id=tmdb_id,
                original_scraped_torrent_title=original_scraped_torrent_title,
                genres=genres, current_score=current_score,
                selected_folder=selected_folder,
                selected_folder_is_custom=selected_folder_is_custom,
                selected_tags=selected_tags,
            )

        if not magnet_link:
            return jsonify({'error': 'No magnet link or URL provided'}), 400

        # Obfuscate the link for logging
        obfuscated_link = obfuscate_magnet_link(magnet_link)
        logging.info(f"Link: {obfuscated_link}")

        temp_file = None
        actual_magnet_to_add = magnet_link # Assume initially it's the one to add

        # If it's a URL rather than a magnet link
        if magnet_link.startswith('http'):
            try:
                # For Jackett URLs or any other torrent URLs, attempt to download
                # but handle redirects to magnet links.
                response = requests.get(magnet_link, timeout=30, allow_redirects=False) # Key change: allow_redirects=False
                
                if response.status_code >= 200 and response.status_code < 300:
                    # Direct download successful
                    with tempfile.NamedTemporaryFile(delete=False, suffix='.torrent') as tmp:
                        tmp.write(response.content)
                        tmp.flush()
                        temp_file = tmp.name
                        logging.info(f"Downloaded torrent file to {temp_file}")
                        actual_magnet_to_add = None # We'll use the temp_file for adding
                elif response.status_code >= 300 and response.status_code < 400 and 'Location' in response.headers:
                    redirect_url = response.headers['Location']
                    if redirect_url.startswith('magnet:'):
                        logging.info(f"HTTP link redirected to magnet link: {redirect_url[:60]}...")
                        actual_magnet_to_add = redirect_url # This is the magnet link to add
                        temp_file = None # No temp file in this case
                    elif redirect_url.startswith('http'):
                        # Handle HTTP to HTTP redirect if necessary, or error out
                        # For simplicity, we'll try to download from the new HTTP URL once.
                        logging.info(f"HTTP link redirected to another HTTP URL: {redirect_url}. Attempting download again.")
                        response = requests.get(redirect_url, timeout=30) # Allow redirects for this second attempt by default
                        response.raise_for_status() # Raise an error if this also fails or redirects badly
                        with tempfile.NamedTemporaryFile(delete=False, suffix='.torrent') as tmp:
                            tmp.write(response.content)
                            tmp.flush()
                            temp_file = tmp.name
                            logging.info(f"Downloaded torrent file from redirected URL to {temp_file}")
                            actual_magnet_to_add = None # Use temp_file
                    else:
                        raise Exception(f"Unhandled redirect location: {redirect_url}")
                else:
                    response.raise_for_status() # Raise an exception for other error codes

            except Exception as e:
                error_message = str(e)
                logging.error(f"Failed to process torrent URL '{magnet_link}': {error_message}")
                if temp_file and os.path.exists(temp_file):
                    try:
                        os.unlink(temp_file)
                    except Exception as e_del:
                        logging.warning(f"Failed to delete temp file {temp_file}: {e_del}")
                return jsonify({'error': error_message}), 400

        # Add magnet/torrent to debrid provider — try each provider in order
        from debrid import get_debrid_providers
        from debrid.base import ProviderUnavailableError
        providers = get_debrid_providers()
        torrent_id = None
        # None on a usenet-only setup; the add loop below simply won't run and we
        # return the "failed to add torrent" error.
        debrid_provider = providers[0] if providers else None  # will be updated to whichever succeeds
        last_error = None
        for _prov in providers:
            try:
                _id = _prov.add_torrent(actual_magnet_to_add, temp_file)
                if _id:
                    torrent_id = _id
                    debrid_provider = _prov
                    logging.info(f"[{_prov.PROVIDER_NAME}] Torrent added: {torrent_id}")
                    break
            except ProviderUnavailableError as _pue:
                last_error = str(_pue)
                if '451' in last_error:
                    logging.warning(f"[{_prov.PROVIDER_NAME}] 451 DMCA — trying next provider")
                    continue
                logging.error(f"[{_prov.PROVIDER_NAME}] ProviderUnavailableError: {last_error}")
                continue
            except Exception as _ex:
                last_error = str(_ex)
                logging.error(f"[{_prov.PROVIDER_NAME}] Error: {last_error}")
                continue
        try:
            if not torrent_id:
                error_message = f"Failed to add torrent to any provider. Last error: {last_error}"
                logging.error(f"Error in add_torrent_to_debrid: {last_error}")
                return jsonify({'error': error_message}), 500

            # Extract torrent hash from magnet link or torrent file
            torrent_hash = None
            if actual_magnet_to_add and actual_magnet_to_add.startswith('magnet:'):
                # Extract hash from magnet link
                hash_match = re.search(r'btih:([a-fA-F0-9]{40})', actual_magnet_to_add)
                if hash_match:
                    torrent_hash = hash_match.group(1).lower()
            elif temp_file:
                # Extract hash from torrent file
                with open(temp_file, 'rb') as f:
                    torrent_data = bencodepy.decode(f.read())
                    info = torrent_data[b'info']
                    torrent_hash = hashlib.sha1(bencodepy.encode(info)).hexdigest()

            # Record the torrent addition only if it hasn't been recorded in the last minute
            if torrent_hash:
                # Check recent history for this hash
                history = get_torrent_history(torrent_hash)
                
                # Prepare item data
                item_data = {
                    'title': title,
                    'year': year,
                    'media_type': media_type,
                    'season': season_number,
                    'episode': episode_number,
                    'version': final_version_for_item, # Use the determined version
                    'tmdb_id': tmdb_id,
                    'genres': genres,
                    'selected_folder': selected_folder,  # Include user-selected folder for symlink mode
                    'selected_folder_is_custom': selected_folder_is_custom,  # Flag for custom vs standard folders
                    'tags': selected_tags  # Plex mode NZB folder routing
                }

                # If there's a recent entry, update it instead of creating new one
                if history:
                    update_torrent_tracking(
                        torrent_hash=torrent_hash,
                        item_data=item_data,
                        trigger_details={
                            'source': 'web_interface',
                            'user_initiated': True
                        },
                        trigger_source='manual_add',
                        rationale='User manually added via web interface'
                    )
                    logging.info(f"Updated existing torrent tracking entry for {title} (hash: {torrent_hash})")
                else:
                    # Record new addition if no history exists
                    record_torrent_addition(
                        torrent_hash=torrent_hash,
                        trigger_source='manual_add',
                        rationale='User manually added via web interface',
                        item_data=item_data,
                        trigger_details={
                            'source': 'web_interface',
                            'user_initiated': True
                        }
                    )
                    logging.info(f"Recorded new torrent addition for {title} with hash {torrent_hash}")

        finally:
            # Clean up temp file if it exists
            if temp_file and os.path.exists(temp_file):
                try:
                    os.unlink(temp_file)
                except Exception as e:
                    logging.warning(f"Failed to delete temp file {temp_file}: {e}")

        # Get torrent info for processing
        # Initialize defaults to avoid UnboundLocalError in any branch
        torrent_info = None
        is_cached = False

        # Prefer capability flags, but always fall back to fetching by ID
        if getattr(debrid_provider, 'supports_direct_cache_check', False):
            # For providers like Real-Debrid, use the torrent ID directly
            torrent_info = debrid_provider.get_torrent_info(torrent_id)
        else:
            # Fallback: still attempt to retrieve info by torrent ID
            try:
                torrent_info = debrid_provider.get_torrent_info(torrent_id)
            except Exception as _:
                torrent_info = None

        # Derive cached status if info is available
        if torrent_info:
            status = torrent_info.get('status', '')
            is_cached = (status == 'downloaded')
        '''
        #tb
        else:
            hash_value = extract_hash_from_magnet(magnet_link) if magnet_link.startswith('magnet:') else None
            if not hash_value and temp_file:
                # If we have a torrent file, extract hash from it
                with open(temp_file, 'rb') as f:
                    torrent_data = bencodepy.decode(f.read())
                    info = torrent_data[b'info']
                    hash_value = hashlib.sha1(bencodepy.encode(info)).hexdigest()
            if not hash_value:
                error_message = "Failed to extract hash from torrent"
                logging.error(error_message)
                return jsonify({'error': error_message}), 500
            torrent_info = debrid_provider.get_torrent_info(hash_value)
        '''

        if not torrent_info:
            error_message = "Failed to get torrent info"
            logging.error(error_message)
            return jsonify({'error': error_message}), 500

        # Process the content — if files are empty (provider hasn't resolved yet),
        # retry a few times before giving up. Debrid-Link in particular can return
        # an empty files list immediately after add while it's still resolving the magnet.
        processor = ContentProcessor()
        success, message = processor.process_content(torrent_info)

        if not success and 'No files found' in message:
            for _retry in range(4):
                import time as _time
                _time.sleep(3)
                try:
                    torrent_info = debrid_provider.get_torrent_info(torrent_id)
                except Exception:
                    pass
                if torrent_info and torrent_info.get('files'):
                    success, message = processor.process_content(torrent_info)
                    if success:
                        logging.info(f"Torrent files resolved on retry {_retry + 1}")
                        break

        if not success:
            logging.error(f"Failed to process torrent content: {message}")
            return jsonify({'error': message}), 400

        # Return cache status to the frontend
        cache_status = {
            'is_cached': is_cached,
            'torrent_id': torrent_id,
            'torrent_hash': torrent_hash
        }
        
        # Check if symlinking is enabled
        if get_setting('File Management', 'file_collection_management') == 'Symlinked/Local' or 1==1:
            try:
                # Convert media type to movie_or_episode format
                movie_or_episode = 'episode' if media_type == 'tv' or media_type == 'show' else 'movie'
                
                # Get IMDB ID from metadata
                imdb_id = None
                if tmdb_id:
                    try:
                        metadata = get_metadata(tmdb_id=int(tmdb_id), item_media_type=media_type)
                        imdb_id = metadata.get('imdb_id')
                        if not imdb_id:
                            # Try to get from database mapping
                            from cli_battery.app.direct_api import DirectAPI
                            imdb_id, _ = DirectAPI.tmdb_to_imdb(str(tmdb_id), media_type='show' if media_type == 'tv' else media_type)
                    except Exception as e:
                        logging.warning(f"Failed to get IMDB ID: {e}")
                
                # Get release date from metadata
                episode_data = {}
                if media_type in ['tv', 'show']:
                    release_date = 'Unknown'  # Initialize to 'Unknown'
                    # For TV shows, get episode-specific release date
                    metadata = get_metadata(tmdb_id=int(tmdb_id), item_media_type=media_type)
                    if metadata and metadata.get('seasons') and season_number is not None:
                        seasons_meta = metadata['seasons']
                        season_data = seasons_meta.get(season_number)
                        if season_data is None:
                            season_data = seasons_meta.get(str(season_number), {})
                        if episode_number is not None:
                            episodes_meta = season_data.get('episodes', {})
                            episode_data = episodes_meta.get(episode_number)
                            if episode_data is None:
                                episode_data = episodes_meta.get(str(episode_number), {})
                        
                        first_aired_str = episode_data.get('first_aired')
                        
                        if first_aired_str:
                            try:
                                # Use iso8601 library for robust parsing
                                first_aired_utc = iso8601.parse_date(first_aired_str)
                                # Ensure it's timezone-aware
                                if first_aired_utc.tzinfo is None:
                                    first_aired_utc = first_aired_utc.replace(tzinfo=timezone.utc)
                                
                                # Convert UTC to local timezone
                                local_tz = _get_local_timezone()
                                premiere_dt_local_tz = first_aired_utc.astimezone(local_tz)
                                
                                # Format the local date
                                release_date = premiere_dt_local_tz.strftime("%Y-%m-%d")
                                logging.info(f"Successfully parsed release date: {release_date}")
                            except (ValueError, iso8601.ParseError) as e:
                                logging.warning(f"Could not parse first_aired_val: '{first_aired_str}' for episode S{season_number}E{episode_number}: {e}")
                else:
                    # For movies, get movie release date
                    metadata = get_metadata(tmdb_id=int(tmdb_id), item_media_type=media_type)
                    if metadata:
                        release_date = get_release_date(metadata, metadata.get('imdb_id'))
                        if not release_date: # Ensure get_release_date didn't return None/empty
                            release_date = 'Unknown'
                    else:
                        release_date = 'Unknown'
                
                # Get the file info for symlinking
                files = torrent_info.get('files', [])
                if not files:
                    raise Exception("No files found in torrent")

                # Get the largest video file
                video_files = [f for f in files if any(f['path'].lower().endswith(ext) for ext in ['.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv'])]
                if not video_files:
                    raise Exception("No video files found in torrent")
                
                largest_file = max(video_files, key=lambda x: x.get('bytes', 0))
                filled_by_file = os.path.basename(largest_file['path'])
                # Get the torrent title from torrent_info's filename
                filled_by_title = torrent_info.get('filename', '') or os.path.basename(os.path.dirname(largest_file['path']))
                debrid_folder_name = torrent_info.get('debrid_folder_name') or filled_by_title

                # Extract resolution from torrent title
                resolution = extract_resolution_from_filename(original_scraped_torrent_title) if original_scraped_torrent_title else None
                if not resolution:
                    # Fallback: try extracting from the actual file name
                    resolution = extract_resolution_from_filename(filled_by_file)
                logging.info(f"Extracted resolution for {title}: {resolution}")

                # Create media item
                item = {
                    'title': title,
                    'year': year,
                    'type': 'episode' if media_type in ['tv', 'show'] else 'movie',
                    'version': final_version_for_item, # Use the determined version
                    'resolution': resolution,
                    'tmdb_id': tmdb_id,
                    'imdb_id': imdb_id,
                    'state': 'Checking',
                    'filled_by_magnet': actual_magnet_to_add,
                    'filled_by_torrent_id': torrent_id,
                    'filled_by_title': filled_by_title,
                    'filled_by_file': filled_by_file,
                    'original_scraped_torrent_title': original_scraped_torrent_title,
                    'release_date': release_date,
                    'genres': json.dumps(genres),  # JSON encode the genres list
                    'current_score': current_score,
                    'real_debrid_original_title': torrent_info.get('original_filename'),
                    'debrid_folder_name': debrid_folder_name,
                    'content_source': 'content_requester',
                    'content_source_detail': current_user.username if (is_user_system_enabled() and current_user.is_authenticated) else 'CD-Discover',
                    'selected_folder': selected_folder,  # User-selected folder from dropdown
                    'selected_folder_is_custom': selected_folder_is_custom,  # Flag for custom vs standard folders
                    'tags': selected_tags  # Plex mode NZB folder routing
                }

                # Add TV show specific fields if this is a TV show
                if media_type in ['tv', 'show']:
                    item.update({
                        'season_number': season_number,
                        'episode_number': episode_number,
                        'episode_title': episode_data.get('title', f'Episode {episode_number}') if episode_number is not None else None
                    })

                # If this is a season pack (has season but no episode number)
                if media_type in ['tv', 'show'] and season_number is not None and episode_number is None:
                    logging.info(f"Processing season pack for {title} Season {season_number}")
                    # Get metadata for all episodes in the season
                    metadata = get_metadata(tmdb_id=int(tmdb_id), item_media_type=media_type)
                    if metadata and metadata.get('seasons'):
                        seasons_meta = metadata['seasons']
                        season_data = seasons_meta.get(season_number)
                        if season_data is None:
                            season_data = seasons_meta.get(str(season_number), {})
                        episodes = season_data.get('episodes', {})
                        
                        # Create a MediaMatcher instance
                        media_matcher = MediaMatcher()
                        
                        # Pre-parse files
                        # Pre-parse all files from the torrent info once
                        parsed_files = []
                        torrent_files = torrent_info.get('files', [])
                        for file_dict in torrent_files:
                            parsed_info = media_matcher._parse_file_info(file_dict)
                            if parsed_info:
                                parsed_files.append(parsed_info)

                        if not parsed_files:
                            logging.warning(f"No valid video files found in torrent for season pack processing for {title} S{season_number}.")
                            # Decide if we should return an error or just continue
                            # For now, let's return success as the torrent was added, but log the issue.
                            return jsonify({
                                'success': True,
                                'message': 'Successfully added torrent, but no valid video files found for season pack processing.',
                                'cache_status': cache_status
                            })

                        # For each episode in the season
                        for episode_num_str, episode_data in episodes.items(): # Renamed episode_num to avoid clash
                            try:
                                episode_num = int(episode_num_str) # Use a different variable name here
                                # Create episode-specific item
                                episode_item = item.copy()
                                episode_item['episode_number'] = episode_num
                                episode_item['current_score'] = current_score # Use the score passed for the pack
                                episode_item['type'] = 'episode' # Ensure type is set for matching
                                # Note: content_source inherited from item which already has 'content_requester'
                                
                                # Get episode-specific release date and title
                                first_aired = episode_data.get('first_aired')
                                episode_item['episode_title'] = episode_data.get('title', f'Episode {episode_num}')
                                
                                if first_aired:
                                    try:
                                        # Use iso8601.parse_date for robust parsing
                                        first_aired_utc = iso8601.parse_date(first_aired)
                                        # Ensure it's timezone-aware (iso8601.parse_date might already return aware)
                                        if first_aired_utc.tzinfo is None or first_aired_utc.tzinfo.utcoffset(first_aired_utc) is None:
                                            first_aired_utc = first_aired_utc.replace(tzinfo=timezone.utc)
                                        
                                        local_tz = _get_local_timezone()
                                        local_dt = first_aired_utc.astimezone(local_tz)
                                        
                                        # Format the local date as string
                                        episode_item['release_date'] = local_dt.strftime("%Y-%m-%d")
                                    except (ValueError, iso8601.ParseError) as e:
                                        episode_item['release_date'] = 'Unknown'
                                        logging.warning(f"Could not parse release date for S{season_number}E{episode_num}, value was: '{first_aired}'. Error: {e}")
                                
                                # Find matching file using find_best_match_from_parsed
                                # Find matching file for this episode from the pre-parsed list
                                match_result = media_matcher.find_best_match_from_parsed(parsed_files, episode_item)
                                
                                if match_result:
                                    matching_filepath_basename, _ = match_result # Unpack the tuple

                                    matched_file_dict = next(
                                        (
                                            file_dict for file_dict in torrent_files
                                            if os.path.basename(file_dict.get('path', '')) == matching_filepath_basename
                                        ),
                                        None
                                    )

                                    episode_item['filled_by_file'] = matching_filepath_basename # Use the basename

                                    if matched_file_dict:
                                        matched_path = matched_file_dict.get('path', '')
                                        matched_folder_name = os.path.basename(os.path.dirname(matched_path))
                                        if matched_folder_name:
                                            episode_item['filled_by_title'] = matched_folder_name
                                            episode_item['debrid_folder_name'] = matched_folder_name
                                        logging.info(
                                            f"Season pack exact match for {title} S{season_number}E{episode_item.get('episode_number')}: "
                                            f"folder='{episode_item.get('debrid_folder_name')}', "
                                            f"file='{episode_item.get('filled_by_file')}'"
                                        )
                                    else:
                                        logging.warning(
                                            f"Matched basename '{matching_filepath_basename}' for {title} "
                                            f"S{season_number}E{episode_item.get('episode_number')} but could not resolve original torrent path."
                                        )

                                    # Add episode to database
                                    from database import add_media_item
                                    episode_id = add_media_item(episode_item, user_initiated=True)
                                    if episode_id:
                                        episode_item['id'] = episode_id
                                        _record_manual_grab(episode_id, 'torrent',
                                                            original_scraped_torrent_title or filled_by_title,
                                                            info_hash=torrent_hash)
                                        # Add to checking queue
                                        from queues.checking_queue import CheckingQueue
                                        checking_queue = CheckingQueue()
                                        checking_queue.add_item(episode_item)
                                    else:
                                         logging.error(f"Failed to add episode S{season_number}E{episode_num} to database.")
                                else:
                                    logging.warning(f"No matching file found for episode S{season_number}E{episode_num} in parsed files.")
                            except Exception as e:
                                logging.error(f"Error processing episode {title} S{season_number}E{episode_num}: {str(e)}", exc_info=True)
                                continue
                    else:
                         logging.warning(f"No metadata or no 'seasons' key found in metadata for TMDB ID {tmdb_id} during season pack processing for {title} S{season_number}.") # Enhanced log

                    # Debrid File Naming: rename the season pack folder in cli_mount DFS
                    # The single rename covers all episodes since they share the same torrent.
                    if torrent_hash:
                        try:
                            from utilities.settings import get_setting as _dbn_gs_sp
                            if _dbn_gs_sp('Debrid Provider', 'enable_debrid_naming', False):
                                _dbn_orig_sp = original_scraped_torrent_title or filled_by_title
                                _dbn_title_sp = _build_debrid_title(
                                    title=title,
                                    year=year,
                                    imdb_id=imdb_id,
                                    version=final_version_for_item,
                                    original_scraped_torrent_title=_dbn_orig_sp,
                                    media_type='tv',
                                    season=season_number,
                                    episode=None,
                                    episode_title=None,
                                    tags=selected_tags or None,
                                    content_source_display_name=None,
                                )
                                if _dbn_title_sp and _dbn_title_sp != _dbn_orig_sp:
                                    import threading as _dbn_t_sp
                                    _dbn_h_sp = torrent_hash
                                    _dbn_n_sp = _dbn_title_sp
                                    _dbn_provider_id_sp = torrent_id  # RD provider ID for DB lookup
                                    def _do_rename_sp(h, name, provider_id=None):
                                        import time as _t
                                        try:
                                            from usenet import get_usenet_client
                                            _dc_sp = get_usenet_client()
                                            from usenet.base import supports, RENAME
                                            if not supports(_dc_sp, RENAME):
                                                return  # active usenet provider (e.g. nzbdav) has no rename semantics
                                            # cli_mount only registers an entry as queryable-by-hash after its
                                            # own periodic sync (default ~10 min) — a 404 in the first several
                                            # attempts is expected, not proof the entry is gone. Only treat 404
                                            # as final once it's persisted for that long (20 attempts x 30s).
                                            _consecutive_404_sp = 0
                                            for _a in range(100):
                                                _renamed_sp, _not_found_sp = _dc_sp.rename_nzb_with_status(h, name)
                                                if _not_found_sp:
                                                    _consecutive_404_sp += 1
                                                    if _consecutive_404_sp >= 20:
                                                        logging.warning(f'[DebridNaming] {h!r} not found in cli_mount (404) for {_consecutive_404_sp} consecutive attempts — giving up (season pack)')
                                                        return
                                                else:
                                                    _consecutive_404_sp = 0
                                                if _renamed_sp:
                                                    logging.info(f'[DebridNaming] Renamed {h!r} -> {name!r} (season pack)')
                                                    # Register cli_debrid IDs — match via magnet infohash
                                                    # (filled_by_torrent_id stores the RD provider ID, not the infohash)
                                                    try:
                                                        import os as _os_sp
                                                        from database.core import get_db_connection as _gdb_sp
                                                        _VIDEO_EXTS_SP = {'.mkv','.mp4','.avi','.mov','.wmv','.m4v','.ts'}
                                                        with _gdb_sp() as _dbc_sp:
                                                            # Use provider_id (RD torrent ID) for lookup — more reliable
                                                            # than infohash since filled_by_magnet may be NULL at rename time
                                                            _lookup = provider_id or h
                                                            _sibs_sp = _dbc_sp.execute(
                                                                "SELECT id, filled_by_file FROM media_items "
                                                                "WHERE (filled_by_torrent_id=? OR filled_by_magnet LIKE ?) "
                                                                "AND state IN ('Checking','Collected','Upgrading')",
                                                                (_lookup, f'%{h}%')
                                                            ).fetchall()
                                                            _cli_ids_sp = {
                                                                s[1]: s[0] for s in _sibs_sp
                                                                if s[1] and _os_sp.path.splitext(s[1])[1].lower() in _VIDEO_EXTS_SP
                                                            }
                                                            if _cli_ids_sp:
                                                                _dc_sp.register_cli_ids(h, _cli_ids_sp)
                                                                logging.info(f'[DebridNaming] Registered {len(_cli_ids_sp)} cli_debrid IDs for {h!r} (season pack)')
                                                                _dc_sp.push_tags_for_item(h, next(iter(_cli_ids_sp.values())))
                                                    except Exception as _reg_sp:
                                                        logging.debug(f'[DebridNaming] cli_ids registration error (season pack): {_reg_sp}')
                                                    return
                                                _t.sleep(30)
                                            logging.warning(f'[DebridNaming] Could not rename {h!r} after 100 attempts (season pack)')
                                        except Exception as _e_sp:
                                            logging.debug(f'[DebridNaming] Rename error (season pack): {_e_sp}')
                                    _dbn_t_sp.Thread(target=_do_rename_sp, args=(_dbn_h_sp, _dbn_n_sp, _dbn_provider_id_sp), daemon=True).start()
                        except Exception as _dbn_ex_sp:
                            logging.debug(f'[DebridNaming] Setup error (season pack): {_dbn_ex_sp}')

                    return jsonify({
                        'success': True,
                        'message': 'Successfully processed season pack',
                        'cache_status': cache_status
                    })
                else:
                    # For single episodes or movies, proceed as normal
                    from database import add_media_item, get_db_connection

                    # When coming from recently_aired, find the existing DB entry and update it
                    # instead of creating a new one — this fixes a missed item without duplicating
                    if source_context == 'recently_aired' and media_type in ['tv', 'show'] and season_number is not None and episode_number is not None:
                        from datetime import datetime as _dt
                        _conn = get_db_connection()
                        try:
                            _row = _conn.execute(
                                '''SELECT id, version FROM media_items
                                   WHERE type = 'episode'
                                   AND (imdb_id = ? OR tmdb_id = ?)
                                   AND season_number = ? AND episode_number = ?
                                   ORDER BY CASE state
                                       WHEN 'Sleeping' THEN 1
                                       WHEN 'Blacklisted' THEN 2
                                       WHEN 'Wanted' THEN 3
                                       ELSE 4
                                   END
                                   LIMIT 1''',
                                (item.get('imdb_id'), item.get('tmdb_id'), season_number, episode_number)
                            ).fetchone()
                            if _row:
                                existing_id = _row['id']
                                logging.info(f"recently_aired fix: updating existing item id={existing_id} (version={_row['version']}) instead of inserting new")
                                _conn.execute(
                                    '''UPDATE media_items SET
                                        state = 'Checking',
                                        filled_by_magnet = ?,
                                        filled_by_torrent_id = ?,
                                        filled_by_title = ?,
                                        filled_by_file = ?,
                                        original_scraped_torrent_title = ?,
                                        debrid_folder_name = ?,
                                        current_score = ?,
                                        ghostlisted = 0,
                                        blacklisted_date = NULL,
                                        sleep_cycles = 0,
                                        last_updated = ?
                                       WHERE id = ?''',
                                    (actual_magnet_to_add, torrent_id, item.get('filled_by_title'),
                                     item.get('filled_by_file'), original_scraped_torrent_title,
                                     item.get('debrid_folder_name'),
                                     current_score, _dt.now(), existing_id)
                                )
                                _conn.commit()
                                item['id'] = existing_id
                                item['version'] = _row['version']
                            else:
                                logging.info(f"recently_aired fix: no existing item found, inserting new")
                                item_id = add_media_item(item, user_initiated=True)
                                if not item_id:
                                    raise Exception("Failed to add item to database")
                                item['id'] = item_id
                        finally:
                            _conn.close()
                    else:
                        item_id = add_media_item(item, user_initiated=True)
                        if not item_id:
                            raise Exception("Failed to add item to database")
                        item['id'] = item_id

                    _record_manual_grab(item['id'], 'torrent',
                                        original_scraped_torrent_title or filled_by_title,
                                        info_hash=torrent_hash)

                    # Add item to checking queue
                    from queues.checking_queue import CheckingQueue
                    checking_queue = CheckingQueue()
                    checking_queue.add_item(item)
                    logging.info(f"Added item to checking queue: {item}")

                    # Debrid File Naming: rename cli_mount DFS folder using structured CLI name
                    if torrent_hash:
                        try:
                            from utilities.settings import get_setting as _dbn_gs2
                            if _dbn_gs2('Debrid Provider', 'enable_debrid_naming', False):
                                _dbn_media2 = item.get('type', '')
                                _dbn_mt2 = 'tv' if _dbn_media2 == 'episode' else _dbn_media2
                                _dbn_season2 = item.get('season_number')
                                _dbn_ep2 = item.get('episode_number')
                                _dbn_orig2 = original_scraped_torrent_title or filled_by_title
                                _dbn_title2 = _build_debrid_title(
                                    title=item.get('title', ''),
                                    year=item.get('year', ''),
                                    imdb_id=item.get('imdb_id'),
                                    version=item.get('version', ''),
                                    original_scraped_torrent_title=_dbn_orig2,
                                    media_type=_dbn_mt2,
                                    season=_dbn_season2,
                                    episode=_dbn_ep2,
                                    episode_title=item.get('episode_title'),
                                    tags=item.get('tags') or None,
                                    content_source_display_name=item.get('content_source_detail') or item.get('content_source'),
                                )
                                if _dbn_title2 and _dbn_title2 != _dbn_orig2:
                                    import threading as _dbn_t2
                                    _dbn_h2, _dbn_n2 = torrent_hash, _dbn_title2
                                    _dbn_item_id2 = item.get('id')
                                    def _do_rename2(h, name, item_id):
                                        import time as _t
                                        try:
                                            from usenet import get_usenet_client
                                            _dc2 = get_usenet_client()
                                            from usenet.base import supports, RENAME
                                            if not supports(_dc2, RENAME):
                                                return  # active usenet provider (e.g. nzbdav) has no rename semantics
                                            # cli_mount only registers an entry as queryable-by-hash after its
                                            # own periodic sync (default ~10 min) — a 404 in the first several
                                            # attempts is expected, not proof the entry is gone. Only treat 404
                                            # as final once it's persisted for that long (20 attempts x 30s).
                                            _consecutive_404_2 = 0
                                            for _a2 in range(100):
                                                _renamed2, _not_found2 = _dc2.rename_nzb_with_status(h, name)
                                                if _not_found2:
                                                    _consecutive_404_2 += 1
                                                    if _consecutive_404_2 >= 20:
                                                        logging.warning(f'[DebridNaming] {h!r} not found in cli_mount (404) for {_consecutive_404_2} consecutive attempts — giving up (manual add)')
                                                        return
                                                else:
                                                    _consecutive_404_2 = 0
                                                if _renamed2:
                                                    logging.info(f'[DebridNaming] Renamed {h!r} -> {name!r} (manual add, attempt {_a2+1} of 100)')
                                                    if item_id:
                                                        try:
                                                            from database.database_writing import update_media_item as _umi
                                                            _umi(item_id, debrid_folder_name=name)
                                                        except Exception as _db_err:
                                                            logging.debug(f'[DebridNaming] DB update failed (manual add): {_db_err}')
                                                        _dc2.register_cli_ids_for_item(h, item_id)
                                                        _dc2.push_tags_for_item(h, item_id)
                                                    return
                                                _t.sleep(30)
                                            logging.warning(f'[DebridNaming] Could not rename {h!r} after 100 attempts (manual add)')
                                        except Exception as _e2:
                                            logging.debug(f'[DebridNaming] Rename error (manual add): {_e2}')
                                    _dbn_t2.Thread(target=_do_rename2, args=(_dbn_h2, _dbn_n2, _dbn_item_id2), daemon=True).start()
                        except Exception as _dbn_ex2:
                            logging.debug(f'[DebridNaming] Setup error (manual add): {_dbn_ex2}')

            except Exception as e:
                logging.error(f"Failed to add item to checking queue: {e}")
                # Don't return error since the main operation succeeded
        
        return jsonify({
            'success': True,
            'message': 'Successfully added torrent to debrid provider and processed content',
            'cache_status': cache_status
        })

    except Exception as e:
        error_message = str(e)
        logging.error(f"Error in add_torrent_to_debrid: {error_message}")
        return jsonify({'error': error_message}), 500
