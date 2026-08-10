from flask import jsonify, request, render_template, session, Blueprint
import copy
import logging
from usenet import get_usenet_provider_display_name as _usenet_pname
from debrid import get_debrid_provider
# Provider-agnostic: avoid direct Real-Debrid import
from .models import user_required, onboarding_required, admin_required, scraper_permission_required, scraper_view_access_required
from utilities.settings import get_setting, get_all_settings, load_config, save_config
from database.database_reading import get_all_season_episode_counts, get_media_item_presence_overall, get_media_items_presence_batch
from utilities.web_scraper import trending_movies, trending_shows, trending_anime, web_scrape, web_scrape_tvshow, process_media_selection, process_torrent_selection
from utilities.web_scraper import get_media_details
from scraper.scraper import scrape
from utilities.manual_scrape import get_details
from utilities.web_scraper import search_trakt
from utilities.local_library_scan import extract_resolution_from_filename
from queues.torrent_processor import TorrentProcessor
from queues.media_matcher import MediaMatcher
from typing import Dict, Any, Tuple
import requests
import tempfile
import os
import bencodepy
from debrid.common.torrent import torrent_to_magnet
import hashlib
from datetime import datetime, timezone, timedelta
from database.torrent_tracking import record_torrent_addition, get_torrent_history, update_torrent_tracking
from flask_login import current_user
from .utils import is_user_system_enabled
import asyncio
from utilities.phalanx_db_cache_manager import PhalanxDBClassManager
import re
import time
import json
from utilities.web_scraper import get_media_meta
from typing import List, Dict, Any, Optional
import iso8601
from utilities.reverse_parser import parse_filename_for_version # Added import
from utilities.tmdb_cache import cache_response, get_from_cache, set_in_cache, get_cached_db_statuses # Caching support
from concurrent.futures import ThreadPoolExecutor, as_completed # Parallel API calls

scraper_bp = Blueprint('scraper', __name__)

# Initialize cache manager only if enabled
_phalanx_cache_manager = PhalanxDBClassManager() if get_setting('UI Settings', 'enable_phalanx_db', default=False) else None

# PHASE 2 OPTIMIZATION: Search analytics for data-driven prefetching
_search_analytics = {}

@scraper_bp.route('/convert_tmdb_to_imdb/<int:tmdb_id>')
def convert_tmdb_to_imdb(tmdb_id):
    from metadata.metadata import get_imdb_id_if_missing
    max_retries = 1
    base_delay = 0.1  # Base delay in seconds
    
    for attempt in range(max_retries):
        try:
            imdb_id = get_imdb_id_if_missing({'tmdb_id': tmdb_id})
            if imdb_id:
                return jsonify({'imdb_id': imdb_id})
            
            # If we get None but no exception, try again with backoff
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)  # Exponential backoff
                logging.warning(f"TMDB to IMDB conversion attempt {attempt + 1} failed, retrying in {delay} seconds...")
                time.sleep(delay)
                continue
                
        except Exception as e:
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)  # Exponential backoff
                logging.error(f"Error in TMDB to IMDB conversion attempt {attempt + 1}: {str(e)}, retrying in {delay} seconds...")
                time.sleep(delay)
                continue
            else:
                logging.error(f"All TMDB to IMDB conversion attempts failed: {str(e)}")
                break
    
    return jsonify({'imdb_id': 'N/A'})

def obfuscate_magnet_link(magnet_link: str) -> str:
    """
    Obfuscate the magnet link by hiding the domain and API key if present.
    """
    # Check if the magnet link contains 'jackett_apikey'
    if 'jackett_apikey' in magnet_link:
        # Use regex to find and replace the domain and API key
        # Replace the domain (e.g., http://192.168.1.51:9117) with '***'
        magnet_link = re.sub(r'^http:\/\/[^\/]+', '***', magnet_link)
        # Replace the jackett_apikey value with '***'
        magnet_link = re.sub(r'jackett_apikey=[^&]+', 'jackett_apikey=***', magnet_link)
    return magnet_link

class ContentProcessor:
    """Handles the processing of media content after it's been added to the debrid service"""
    
    def __init__(self):
        self.media_matcher = MediaMatcher()

    def process_content(self, torrent_info: Dict[str, Any], item: Dict[str, Any] = None) -> Tuple[bool, str]:
        """
        Process content after it's been added to the debrid service
        
        Args:
            torrent_info: Information about the added torrent
            item: Optional media item to match against
            
        Returns:
            Tuple of (success, message)
        """
        try:
            files = torrent_info.get('files', [])
            if not files:
                return False, "No files found in torrent"

            # If we have a specific item to match against
            if item:
                matches = self.media_matcher.match_content(files, item)
                if not matches:
                    return False, "No matching files found"
                if len(matches) > 1 and item.get('type') == 'movie':
                    return False, "Multiple matches found for movie"
            # Otherwise just check for any valid video files
            else:
                video_files = [f for f in files if self._is_video_file(f.get('path', ''))]
                if not video_files:
                    return False, "No suitable video files found"

            return True, "Content processed successfully"
            
        except Exception as e:
            logging.error(f"Error processing content: {str(e)}")
            return False, f"Error processing content: {str(e)}"

    def _is_video_file(self, file_path: str) -> bool:
        """Check if a file is a video file based on extension"""
        video_extensions = {'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv'}
        return any(file_path.lower().endswith(ext) for ext in video_extensions)

def _download_and_get_hash(url: str) -> str:
    """
    Download a torrent file from URL and extract its hash
    
    Args:
        url: URL to download torrent from
        
    Returns:
        Torrent hash string
    
    Raises:
        Exception if download fails or hash cannot be extracted
    """
    try:
        # Download the file
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        
        try:
            # Parse the torrent data directly from response content
            torrent = bencodepy.decode(response.content)
            info = torrent[b'info']
            encoded_info = bencodepy.encode(info)
            import hashlib
            torrent_hash = hashlib.sha1(encoded_info).hexdigest()
            return torrent_hash
            
        except Exception as e:
            raise Exception(f"Failed to decode torrent data: {str(e)}")
                
    except Exception as e:
        raise Exception(f"Failed to process torrent URL: {str(e)}")

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

        # --- START EDIT: Get current_score from form data ---
        current_score_str = request.form.get('current_score', '0') # Default to '0'
        try:
            current_score = float(current_score_str)
        except (ValueError, TypeError):
            logging.warning(f"Invalid current_score value received: '{current_score_str}'. Defaulting to 0.")
            current_score = 0.0
        # --- END EDIT ---

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

        # Convert season and episode to integers or None
        try:
            season_number = int(season) if season and season.lower() != 'null' else None
        except (ValueError, TypeError):
            season_number = None

        try:
            episode_number = int(episode) if episode and episode.lower() != 'null' else None
        except (ValueError, TypeError):
            episode_number = None
            
        # NZB / Usenet path — route to cli_mount instead of debrid
        protocol = request.form.get('protocol', '').lower()
        nzb_url = request.form.get('nzb_url', '')
        episode_nzb_urls_raw = request.form.get('episode_nzb_urls', '')
        fallback_nzb_urls_raw = request.form.get('fallback_nzb_urls', '')
        episode_filenames_raw = request.form.get('episode_filenames', '')
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
                if media_type in ['tv', 'show']:
                    release_date = 'Unknown'  # Initialize to 'Unknown'
                    # For TV shows, get episode-specific release date
                    metadata = get_metadata(tmdb_id=int(tmdb_id), item_media_type=media_type)
                    if metadata and metadata.get('seasons'):
                        # Use integer keys directly since that's how the data is structured
                        season_data = metadata['seasons'].get(season_number, {})
                        
                        # Use integer keys for episodes as well
                        episode_data = season_data.get('episodes', {}).get(episode_number, {})
                        
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
                        'episode_title': episode_data.get('title', f'Episode {episode_number}') if episode_number else None
                    })

                # If this is a season pack (has season but no episode number)
                if media_type in ['tv', 'show'] and season_number is not None and episode_number is None:
                    logging.info(f"Processing season pack for {title} Season {season_number}")
                    # Get metadata for all episodes in the season
                    metadata = get_metadata(tmdb_id=int(tmdb_id), item_media_type=media_type)
                    if metadata and metadata.get('seasons'):
                        # --- START EDIT: Use integer season_number as key ---
                        season_data = metadata['seasons'].get(season_number, {}) # Use integer season_number directly
                        # --- END EDIT ---
                        episodes = season_data.get('episodes', {})
                        
                        # Create a MediaMatcher instance
                        media_matcher = MediaMatcher()
                        
                        # --- START EDIT: Pre-parse files ---
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
                        # --- END EDIT ---

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
                                        # --- START EDIT: Use iso8601.parse_date for robust parsing ---
                                        first_aired_utc = iso8601.parse_date(first_aired)
                                        # Ensure it's timezone-aware (iso8601.parse_date might already return aware)
                                        if first_aired_utc.tzinfo is None or first_aired_utc.tzinfo.utcoffset(first_aired_utc) is None:
                                            first_aired_utc = first_aired_utc.replace(tzinfo=timezone.utc)
                                        
                                        local_tz = _get_local_timezone()
                                        local_dt = first_aired_utc.astimezone(local_tz)
                                        
                                        # Format the local date as string
                                        episode_item['release_date'] = local_dt.strftime("%Y-%m-%d")
                                    except (ValueError, iso8601.ParseError) as e:
                                        # --- END EDIT ---
                                        episode_item['release_date'] = 'Unknown'
                                        logging.warning(f"Could not parse release date for S{season_number}E{episode_num}, value was: '{first_aired}'. Error: {e}")
                                
                                # --- START EDIT: Find matching file using find_best_match_from_parsed ---
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
                                        # Add to checking queue
                                        from queues.checking_queue import CheckingQueue
                                        checking_queue = CheckingQueue()
                                        checking_queue.add_item(episode_item)
                                    else:
                                         logging.error(f"Failed to add episode S{season_number}E{episode_num} to database.")
                                else:
                                    logging.warning(f"No matching file found for episode S{season_number}E{episode_num} in parsed files.")
                                # --- END EDIT ---
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
                                            from usenet.climount_client import get_climount_client
                                            _dc_sp = get_climount_client()
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
                                            from usenet.climount_client import get_climount_client
                                            _dc2 = get_climount_client()
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

@scraper_bp.route('/movies_trending', methods=['GET', 'POST'])
@user_required
@scraper_view_access_required
def movies_trending():
    from utilities.web_scraper import get_available_versions
    # --- Import database reading function ---
    from database.database_reading import get_media_item_presence_overall
    # --- End import ---

    versions = get_available_versions()
    is_requester = current_user.is_authenticated and current_user.role == 'requester'
    
    if request.method == 'GET':
        trendingMoviesData = trending_movies() # Rename original data
        if trendingMoviesData and 'trendingMovies' in trendingMoviesData:
            processed_movies = []
            for item in trendingMoviesData['trendingMovies']:
                tmdb_id = item.get('tmdb_id')
                if tmdb_id:
                    try:
                        tmdb_id_int = int(tmdb_id)
                        db_state = get_media_item_presence_overall(tmdb_id=tmdb_id_int)
                    except (ValueError, TypeError):
                        db_state = 'Missing'

                    # Map state to frontend status
                    if db_state == 'Collected':
                        item['db_status'] = 'collected'
                    elif db_state == 'Partial':
                        item['db_status'] = 'partial'
                    elif db_state == 'Blacklisted':
                        item['db_status'] = 'blacklisted'
                    elif db_state not in ['Missing', 'Ignored', None]: 
                        item['db_status'] = 'processing'
                    else:
                        item['db_status'] = 'missing'
                else:
                    item['db_status'] = 'missing' # Default if no ID
                processed_movies.append(item)
                
            # Return processed data under the original key
            return jsonify({'trendingMovies': processed_movies})
        else:
            # Return original error structure or a default one
            return jsonify(trendingMoviesData if trendingMoviesData else {'error': 'Error retrieving trending movies'})
            
    return render_template('scraper.html', versions=versions, is_requester=is_requester)

@scraper_bp.route('/shows_trending', methods=['GET', 'POST'])
@user_required
@scraper_view_access_required
def shows_trending():
    from utilities.web_scraper import get_available_versions

    versions = get_available_versions()
    is_requester = current_user.is_authenticated and current_user.role == 'requester'
    
    if request.method == 'GET':
        trendingShowsData = trending_shows() # Rename original data
        if trendingShowsData and 'trendingShows' in trendingShowsData:
            processed_shows = []
            for item in trendingShowsData['trendingShows']:
                tmdb_id = item.get('tmdb_id')
                if tmdb_id:
                    try:
                        tmdb_id_int = int(tmdb_id)
                        db_state = get_media_item_presence_overall(tmdb_id=tmdb_id_int)
                    except (ValueError, TypeError):
                         db_state = 'Missing'
                         
                    # Map state to frontend status
                    if db_state == 'Collected':
                        item['db_status'] = 'collected'
                    elif db_state == 'Partial':
                        item['db_status'] = 'partial'
                    elif db_state == 'Blacklisted':
                        item['db_status'] = 'blacklisted'
                    elif db_state not in ['Missing', 'Ignored', None]: 
                        item['db_status'] = 'processing'
                    else:
                        item['db_status'] = 'missing'
                else:
                    item['db_status'] = 'missing' # Default if no ID
                processed_shows.append(item)
                
            # Return processed data under the original key
            return jsonify({'trendingShows': processed_shows})
        else:
            # Return original error structure or a default one
            return jsonify(trendingShowsData if trendingShowsData else {'error': 'Error retrieving trending shows'})
            
    return render_template('scraper.html', versions=versions, is_requester=is_requester)

@scraper_bp.route('/anime_trending', methods=['GET', 'POST'])
@user_required
@scraper_view_access_required
def anime_trending():
    from utilities.web_scraper import get_available_versions

    versions = get_available_versions()
    is_requester = current_user.is_authenticated and current_user.role == 'requester'

    if request.method == 'GET':
        trendingAnimeData = trending_anime() # Get trending anime data
        if trendingAnimeData and 'trendingAnime' in trendingAnimeData:
            processed_anime = []
            for item in trendingAnimeData['trendingAnime']:
                tmdb_id = item.get('tmdb_id')
                if tmdb_id:
                    try:
                        tmdb_id_int = int(tmdb_id)
                        db_state = get_media_item_presence_overall(tmdb_id=tmdb_id_int)
                    except (ValueError, TypeError):
                         db_state = 'Missing'

                    # Map state to frontend status
                    if db_state == 'Collected':
                        item['db_status'] = 'collected'
                    elif db_state == 'Partial':
                        item['db_status'] = 'partial'
                    elif db_state == 'Blacklisted':
                        item['db_status'] = 'blacklisted'
                    elif db_state not in ['Missing', 'Ignored', None]:
                        item['db_status'] = 'processing'
                    else:
                        item['db_status'] = 'missing'
                else:
                    item['db_status'] = 'missing' # Default if no ID
                processed_anime.append(item)

            # Return processed data under the original key
            return jsonify({'trendingAnime': processed_anime})
        else:
            # Return original error structure or a default one
            return jsonify(trendingAnimeData if trendingAnimeData else {'error': 'Error retrieving trending anime'})

    return render_template('scraper.html', versions=versions, is_requester=is_requester)

def fetch_all_trending_parallel():
    """
    Fetch all trending data in parallel for faster loading
    Uses ThreadPoolExecutor to make API calls simultaneously
    """
    start_time = time.time()

    # Try cache first
    cache_key = 'scraper:all_trending:v2'
    cached_data = get_from_cache(cache_key)
    if cached_data:
        logging.info(f"✅ Cache HIT: all_trending ({time.time() - start_time:.0f}ms)")
        return cached_data

    logging.info("📡 Cache MISS: Fetching trending data in parallel...")

    results = {
        'trendingMovies': [],
        'trendingShows': [],
        'trendingAnime': []
    }

    # Fetch all three in parallel
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            'movies': executor.submit(trending_movies),
            'shows': executor.submit(trending_shows),
            'anime': executor.submit(trending_anime)
        }

        for key, future in futures.items():
            try:
                data = future.result(timeout=10)  # 10 second timeout
                if key == 'movies' and data and 'trendingMovies' in data:
                    results['trendingMovies'] = data['trendingMovies']
                elif key == 'shows' and data and 'trendingShows' in data:
                    results['trendingShows'] = data['trendingShows']
                elif key == 'anime' and data and 'trendingAnime' in data:
                    results['trendingAnime'] = data['trendingAnime']
            except Exception as e:
                logging.error(f"Error fetching trending {key}: {e}")

    # Cache for 60 seconds
    set_in_cache(cache_key, results, 60)

    duration = (time.time() - start_time) * 1000
    logging.info(f"✅ Fetched all trending in parallel: {duration:.0f}ms")

    return results

@scraper_bp.route('/all_trending', methods=['GET'])
@user_required
@scraper_view_access_required
def all_trending():
    """Combined endpoint that returns all trending data in a single request - NOW WITH PARALLEL FETCHING"""
    from routes.poster_cache import get_cached_trending_response, cache_trending_response

    try:
        # OPTIMIZATION: Use parallel fetching with caching
        trending_data = fetch_all_trending_parallel()

        # Extract data from parallel fetch results
        trendingMoviesData = {'trendingMovies': trending_data.get('trendingMovies', [])}
        trendingShowsData = {'trendingShows': trending_data.get('trendingShows', [])}
        trendingAnimeData = {'trendingAnime': trending_data.get('trendingAnime', [])}

        # Collect all items
        all_items = []
        all_items.extend(trending_data.get('trendingMovies', []))
        all_items.extend(trending_data.get('trendingShows', []))
        all_items.extend(trending_data.get('trendingAnime', []))

        # Extract all TMDB IDs for batch lookup
        tmdb_ids = []
        for item in all_items:
            tmdb_id = item.get('tmdb_id')
            if tmdb_id:
                try:
                    tmdb_ids.append(int(tmdb_id))
                except (ValueError, TypeError):
                    pass

        # Single batch database query for all trending items
        db_statuses = get_media_items_presence_batch(tmdb_ids) if tmdb_ids else {}

        # Helper function to process trending items with db_status (using batch results)
        def process_trending_items(items):
            processed = []
            for item in items:
                tmdb_id = item.get('tmdb_id')
                if tmdb_id:
                    try:
                        tmdb_id_int = int(tmdb_id)
                        db_state = db_statuses.get(tmdb_id_int, 'Missing')
                    except (ValueError, TypeError):
                        db_state = 'Missing'

                    # Map state to frontend status
                    if db_state == 'Collected':
                        item['db_status'] = 'collected'
                    elif db_state == 'Partial':
                        item['db_status'] = 'partial'
                    elif db_state == 'Blacklisted':
                        item['db_status'] = 'blacklisted'
                    elif db_state not in ['Missing', 'Ignored', None]:
                        item['db_status'] = 'processing'
                    else:
                        item['db_status'] = 'missing'
                else:
                    item['db_status'] = 'missing'
                processed.append(item)
            return processed

        # Process each type
        processed_movies = []
        if trendingMoviesData and 'trendingMovies' in trendingMoviesData:
            processed_movies = process_trending_items(trendingMoviesData['trendingMovies'])

        processed_shows = []
        if trendingShowsData and 'trendingShows' in trendingShowsData:
            processed_shows = process_trending_items(trendingShowsData['trendingShows'])

        processed_anime = []
        if trendingAnimeData and 'trendingAnime' in trendingAnimeData:
            processed_anime = process_trending_items(trendingAnimeData['trendingAnime'])

        # Combine all trending data
        combined_result = {
            'trendingMovies': processed_movies,
            'trendingShows': processed_shows,
            'trendingAnime': processed_anime
        }

        # Cache the response for 15 minutes
        cache_trending_response(combined_result)

        return jsonify(combined_result)

    except Exception as e:
        logging.error(f"Error in all_trending endpoint: {e}", exc_info=True)
        # Return empty arrays with proper structure instead of HTML error page
        return jsonify({
            'error': 'Failed to fetch trending data',
            'trendingMovies': [],
            'trendingShows': [],
            'trendingAnime': []
        }), 500

@scraper_bp.route('/', methods=['GET', 'POST'])
@user_required
@scraper_view_access_required
@onboarding_required
def index():
    from utilities.web_scraper import get_available_versions, search_trakt_fast, parse_search_term

    versions = get_available_versions()
    # Check if the user is a requester
    is_requester = current_user.is_authenticated and current_user.role == 'requester'

    if request.method == 'POST':
        search_term = request.form.get('search_term')
        version = request.form.get('version')
        if search_term:
            session['search_term'] = search_term  # Store the search term in the session
            session['version'] = version  # Store the version in the session

            # Parse search term to extract year if present
            base_title, season, episode, year, multi = parse_search_term(search_term)

            # Use fast search to get up to 100 results quickly
            results = search_trakt_fast(base_title, year)
            
            # --- Add status check ---
            if isinstance(results, list):
                processed_results = []
                for item in results:
                    # Assuming 'id' is the TMDB ID in search results
                    tmdb_id = item.get('id') 
                    if tmdb_id:
                        try:
                            # Ensure tmdb_id is integer if needed by the function
                            tmdb_id_int = int(tmdb_id) 
                            db_state = get_media_item_presence_overall(tmdb_id=tmdb_id_int)
                        except (ValueError, TypeError):
                             db_state = 'Missing' # Handle cases where ID might not be numeric
                        
                        # Map state to frontend status
                        if db_state == 'Collected':
                            item['db_status'] = 'collected'
                        elif db_state == 'Partial':
                            item['db_status'] = 'partial'
                        elif db_state == 'Blacklisted':
                            item['db_status'] = 'blacklisted'
                        elif db_state not in ['Missing', 'Ignored', None]: 
                            item['db_status'] = 'processing'
                        else:
                            item['db_status'] = 'missing'
                    else:
                        item['db_status'] = 'missing' # Default if no ID
                    processed_results.append(item)
                results = processed_results
            # --- End status check ---
                
            return jsonify({'results': results})  # Wrap results in a dictionary here
        else:
            return jsonify({'error': 'No search term provided'})
    
    # For GET requests, check if TMDB API key is set
    tmdb_api_key = get_setting('TMDB', 'api_key', '')
    tmdb_api_key_set = bool(tmdb_api_key)

    # SSR OPTIMIZATION: Fetch trending data server-side for instant page load
    trending_data_ssr = None
    try:
        logging.info("🚀 SSR: Fetching trending data for initial page load...")
        start_time = time.time()

        # Use our parallel cached fetcher
        # IMPORTANT: Deep copy the cached data to avoid mutating the cache
        # When we add db_status to items, we don't want those modifications
        # to persist in the cache (which would show stale db_status on subsequent requests)
        trending_raw = copy.deepcopy(fetch_all_trending_parallel())

        # Get all TMDB IDs for batch database lookup
        all_items = []
        all_items.extend(trending_raw.get('trendingMovies', []))
        all_items.extend(trending_raw.get('trendingShows', []))
        all_items.extend(trending_raw.get('trendingAnime', []))

        tmdb_ids = []
        for item in all_items:
            tmdb_id = item.get('tmdb_id')
            if tmdb_id:
                try:
                    tmdb_ids.append(int(tmdb_id))
                except (ValueError, TypeError):
                    pass

        # Single batch database query for all trending items
        db_statuses = get_media_items_presence_batch(tmdb_ids) if tmdb_ids else {}

        # Helper function to add db_status to items
        def add_db_status(items):
            for item in items:
                tmdb_id = item.get('tmdb_id')
                if tmdb_id:
                    try:
                        tmdb_id_int = int(tmdb_id)
                        db_state = db_statuses.get(tmdb_id_int, 'Missing')
                    except (ValueError, TypeError):
                        db_state = 'Missing'

                    # Map state to frontend status
                    if db_state == 'Collected':
                        item['db_status'] = 'collected'
                    elif db_state == 'Partial':
                        item['db_status'] = 'partial'
                    elif db_state == 'Blacklisted':
                        item['db_status'] = 'blacklisted'
                    elif db_state not in ['Missing', 'Ignored', None]:
                        item['db_status'] = 'processing'
                    else:
                        item['db_status'] = 'missing'
                else:
                    item['db_status'] = 'missing'
            return items

        # Process all three types
        trending_data_ssr = {
            'trendingMovies': add_db_status(trending_raw.get('trendingMovies', [])),
            'trendingShows': add_db_status(trending_raw.get('trendingShows', [])),
            'trendingAnime': add_db_status(trending_raw.get('trendingAnime', []))
        }

        duration = (time.time() - start_time) * 1000
        logging.info(f"✅ SSR: Trending data prepared in {duration:.0f}ms")

    except Exception as e:
        logging.error(f"❌ SSR: Failed to fetch trending data: {e}")
        # Fallback to client-side if SSR fails
        trending_data_ssr = None

    # Pass the is_requester flag and SSR data to the template
    return render_template('scraper.html',
                         versions=versions,
                         tmdb_api_key_set=tmdb_api_key_set,
                         is_requester=is_requester,
                         trending_data_ssr=trending_data_ssr,
                         enable_ssr=True)

@scraper_bp.route('/live_search', methods=['POST'])
@user_required
@scraper_view_access_required
def live_search():
    """
    Ultra-fast live search endpoint for instant results.
    Uses web_scrape_lite() which skips heavy TMDB metadata fetching.
    """
    from utilities.web_scraper import web_scrape_lite

    data = request.get_json()
    search_term = data.get('search_term', '')
    limit = data.get('limit', 30)  # PHASE 2: Progressive loading - default 30 results

    if not search_term:
        return jsonify({'error': 'No search term provided'}), 400

    # PHASE 2 OPTIMIZATION: Track search analytics
    _search_analytics[search_term] = _search_analytics.get(search_term, 0) + 1

    # Use lite version for fast results with optional limit
    results = web_scrape_lite(search_term, limit=limit)

    # Add status check for each result - OPTIMIZED: Use batch query instead of N+1
    if isinstance(results, list):
        # PHASE 1 FIX #1: Collect all tmdb_ids first for batch query
        tmdb_ids = []
        for item in results:
            if item.get('id'):
                try:
                    tmdb_ids.append(int(item.get('id')))
                except (ValueError, TypeError):
                    pass

        # Single batch database query with caching (20x → 1x query, cached on repeat)
        db_states = get_cached_db_statuses(tmdb_ids) if tmdb_ids else {}

        # Map results back to items and normalize field names
        processed_results = []
        tmdb_api_key_set = bool(get_setting('TMDB', 'api_key'))

        for item in results:
            tmdb_id = item.get('id')
            if tmdb_id:
                try:
                    tmdb_id_int = int(tmdb_id)
                    db_state = db_states.get(tmdb_id_int, 'Missing')
                except (ValueError, TypeError):
                    db_state = 'Missing'

                # Map state to frontend status
                if db_state == 'Collected':
                    item['db_status'] = 'collected'
                elif db_state == 'Partial':
                    item['db_status'] = 'partial'
                elif db_state == 'Blacklisted':
                    item['db_status'] = 'blacklisted'
                elif db_state not in ['Missing', 'Ignored', None]:
                    item['db_status'] = 'processing'
                else:
                    item['db_status'] = 'missing'
            else:
                item['db_status'] = 'missing'

            # Normalize field names to match trending format EXACTLY
            # Trending has: tmdb_id, rating, vote_average, watcher_count, tmdb_api_key_set
            # Search has: id, vote_average

            # 1. Ensure tmdb_id exists (search returns 'id')
            if 'id' in item and 'tmdb_id' not in item:
                item['tmdb_id'] = item['id']

            # 2. Ensure rating exists (Trakt rating, search has vote_average)
            if 'vote_average' in item and 'rating' not in item:
                item['rating'] = item['vote_average']

            # 3. Ensure vote_average exists (keep if present)
            if 'vote_average' not in item and 'rating' in item:
                item['vote_average'] = item['rating']

            # 4. Add tmdb_api_key_set (frontend flag)
            item['tmdb_api_key_set'] = tmdb_api_key_set

            # 5. Add watcher_count (search doesn't have this, default to 0)
            if 'watcher_count' not in item:
                item['watcher_count'] = 0

            # Note: backdrop_path and genre_ids are now populated directly by search_trakt_fast()
            # from the TMDB API response (no extra API call needed)

            # PHASE 2 OPTIMIZATION: Remove only None values (keep empty strings for compatibility)
            cleaned_item = {k: v for k, v in item.items() if v is not None}
            processed_results.append(cleaned_item)
        results = processed_results

    return jsonify({'results': results})

@scraper_bp.route('/search_analytics', methods=['GET'])
@admin_required
def search_analytics():
    """
    PHASE 2 OPTIMIZATION: View search analytics to identify popular searches.
    Admin-only endpoint for monitoring search patterns.
    """
    # Sort by frequency (most searched first)
    sorted_analytics = sorted(_search_analytics.items(), key=lambda x: x[1], reverse=True)

    # Top 20 searches
    top_searches = [{'term': term, 'count': count} for term, count in sorted_analytics[:20]]

    return jsonify({
        'total_unique_searches': len(_search_analytics),
        'total_searches': sum(_search_analytics.values()),
        'top_searches': top_searches
    })

@scraper_bp.route('/optimization_status', methods=['GET'])
def optimization_status():
    """
    Quick diagnostic endpoint to verify optimizations are loaded.
    Access: /scraper/optimization_status
    """
    import sys
    import inspect

    # Check if get_cached_db_statuses function exists
    try:
        from utilities.tmdb_cache import get_cached_db_statuses, get_cache_stats
        has_cached_function = True
        cache_stats = get_cache_stats()
    except ImportError:
        has_cached_function = False
        cache_stats = {}

    # Check if web_scrape_lite has limit parameter
    from utilities.web_scraper import web_scrape_lite
    sig = inspect.signature(web_scrape_lite)
    has_limit_param = 'limit' in sig.parameters

    # Check analytics tracking
    has_analytics = '_search_analytics' in globals()

    return jsonify({
        'optimization_status': 'ENABLED' if all([
            has_cached_function,
            has_limit_param,
            has_analytics
        ]) else 'PARTIAL',
        'checks': {
            'batch_caching': has_cached_function,
            'progressive_loading': has_limit_param,
            'analytics_tracking': has_analytics
        },
        'cache_stats': cache_stats,
        'analytics_count': len(_search_analytics) if has_analytics else 0,
        'message': 'All optimizations loaded!' if all([has_cached_function, has_limit_param, has_analytics])
                   else 'Some optimizations missing - restart app may be needed'
    })

@scraper_bp.route('/select_season', methods=['GET', 'POST'])
@user_required
@scraper_view_access_required
def select_season():
    from utilities.web_scraper import get_available_versions

    versions = get_available_versions()
    # Check if the user is a requester
    is_requester = current_user.is_authenticated and current_user.role == 'requester'
    
    if request.method == 'POST':
        media_id = request.form.get('media_id')
        title = request.form.get('title')
        year = request.form.get('year')
        # Get allow_specials flag from form data
        allow_specials = request.form.get('allow_specials', 'false').lower() == 'true'
        
        if media_id:
            try:
                # Allow both requesters and regular users to get season data for browsing
                # Pass the allow_specials flag to web_scrape_tvshow
                results = web_scrape_tvshow(media_id, title, year, season=None, allow_specials=allow_specials)
                if not results:
                    return jsonify({'error': 'No results found'}), 404
                elif 'error' in results:
                    return jsonify({'error': results['error']}), 404
                elif 'episode_results' not in results or not results['episode_results']:
                    return jsonify({'error': 'No episode results found'}), 404
                    
                session['show_results'] = results
                return jsonify(results)
            except Exception as e:
                logging.error(f"Error in select_season: {str(e)}", exc_info=True)
                return jsonify({'error': str(e)}), 500
        else:
            return jsonify({'error': 'No media_id provided'}), 400
    
    return render_template('scraper.html', versions=versions, is_requester=is_requester)

@scraper_bp.route('/select_episode', methods=['GET', 'POST'])
@user_required
@scraper_view_access_required
def select_episode():
    from utilities.web_scraper import get_available_versions
    
    versions = get_available_versions()
    # Check if the user is a requester
    is_requester = current_user.is_authenticated and current_user.role == 'requester'
    
    if request.method == 'POST':
        media_id = request.form.get('media_id')
        season = request.form.get('season')
        title = request.form.get('title')
        year = request.form.get('year')
        # Get allow_specials flag from form data
        allow_specials = request.form.get('allow_specials', 'false').lower() == 'true'
        
        logging.info(f"select_episode route received: media_id={media_id}, season={season}, title={title}, year={year}, allow_specials={allow_specials}")
        
        if media_id:
            try:
                # Allow episode data to be retrieved for both requesters and regular users
                # Pass the allow_specials flag to web_scrape_tvshow
                episodeResults = web_scrape_tvshow(media_id, title, year, season, allow_specials=allow_specials)
                if not episodeResults:
                    return jsonify({'error': 'No results found'}), 404
                elif 'error' in episodeResults:
                    return jsonify({'error': episodeResults['error']}), 404
                elif 'episode_results' not in episodeResults or not episodeResults['episode_results']:
                    return jsonify({'error': 'No episode results found'}), 404
                
                # Ensure each episode has required fields
                for episode in episodeResults['episode_results']:
                    if 'vote_average' not in episode:
                        episode['vote_average'] = 0.0
                    if 'still_path' not in episode:
                        episode['still_path'] = episode.get('poster_path')
                    if 'episode_title' not in episode:
                        episode['episode_title'] = f"Episode {episode.get('episode_num', '?')}"
                        
                return jsonify(episodeResults)
            except Exception as e:
                logging.error(f"Error in select_episode: {str(e)}", exc_info=True)
                return jsonify({'error': str(e)}), 500
        else:
            return jsonify({'error': 'No media_id provided'}), 400
    
    return render_template('scraper.html', versions=versions, is_requester=is_requester)

@scraper_bp.route('/select_media', methods=['POST'])
@user_required
@scraper_view_access_required  # Changed from scraper_permission_required to allow requesters to view but not scrape
def select_media():
    from metadata.metadata import get_metadata
    try:
        # Check if the user is a requester and block the scraping action if true
        is_requester = current_user.is_authenticated and current_user.role == 'requester'
        if is_requester:
            return jsonify({
                'error': 'As a Requester, you can view content but cannot perform scraping actions.',
                'torrent_results': [],
                'filtered_out_torrent_results': [] # Ensure this key is present
            }), 403  # 403 Forbidden status code
            
        media_id = request.form.get('media_id')
        title = request.form.get('title')
        year = request.form.get('year')
        media_type = request.form.get('media_type')
        season = request.form.get('season')
        episode = request.form.get('episode')
        multi = request.form.get('multi', 'false').lower() == 'true'
        version = request.form.get('version', 'default')
        genre_ids = request.form.get('genre_ids', '')
        
        skip_cache_check = request.form.get('skip_cache_check', 'false').lower() == 'true'
        background_check = request.form.get('background_check', 'true').lower() == 'true'
        
        logging.info(f"Select media: {media_id}, {title}, {year}, {media_type}, S{season or 'None'}E{episode or 'None'}, multi={multi}, version={version}")
        
        # --- START EDIT: Store imdb_id in session ---
        if media_id:
            try:
                from metadata.metadata import get_imdb_id_if_missing
                # Convert media_type to the format expected by the function
                api_media_type = 'show' if media_type == 'tv' else media_type
                imdb_id = get_imdb_id_if_missing({'tmdb_id': int(media_id), 'media_type': api_media_type})
                if imdb_id:
                    session['last_selected_imdb_id'] = imdb_id
                    logging.info(f"Stored imdb_id {imdb_id} in session for tmdb_id {media_id} with media_type {api_media_type}.")
                else:
                    # Clear session key if lookup fails to prevent using a stale ID
                    if 'last_selected_imdb_id' in session:
                        del session['last_selected_imdb_id']
                    logging.warning(f"Could not resolve imdb_id for tmdb_id {media_id} with media_type {api_media_type}. Cache check might be affected.")
            except (ValueError, TypeError) as e:
                logging.error(f"Error resolving imdb_id from tmdb_id '{media_id}': {e}")
        # --- END EDIT ---

        logging.info(f"Cache check settings: skip_cache_check={skip_cache_check}, background_check={background_check}")
        logging.debug(f"[select_media_route] Calling process_media_selection for '{title}'.")
        
        if not media_id or not title or not year or not media_type:
            return jsonify({'error': 'Missing required parameters'}), 400
            
        if season:
            season = int(season)
        if episode:
            episode = int(episode)
            
        genres = []
        if genre_ids:
            try:
                genres = [int(g) for g in genre_ids.split(',') if g]
            except ValueError:
                genres = [g.strip() for g in genre_ids.split(',') if g.strip()]
                logging.info(f"Using genre names: {genres}")
                
        # --- MODIFICATION: Assume process_media_selection now returns two lists ---
        # result variable is a tuple: (passed_results, filtered_out_results_list)
        # or an error dictionary.
        result_tuple_or_error = process_media_selection(
            media_id, 
            title, 
            year, 
            media_type, 
            season, 
            episode, 
            multi, 
            version, 
            genres,
            skip_cache_check=skip_cache_check,
            background_cache_check=background_check
        )
        
        # Check if there was an error
        if isinstance(result_tuple_or_error, dict) and 'error' in result_tuple_or_error:
            logging.info(f"select_media: process_media_selection returned an error: {result_tuple_or_error.get('error')}")
            # Ensure filtered_out_torrent_results is an empty list in case of error, if it's expected by frontend
            if 'filtered_out_torrent_results' not in result_tuple_or_error:
                 result_tuple_or_error['filtered_out_torrent_results'] = []
            return jsonify(result_tuple_or_error), 400
        
        # Unpack the tuple if no error
        try:
            passed_results, filtered_out_results_list = result_tuple_or_error
            logging.info(f"select_media: process_media_selection returned {len(passed_results)} passed and {len(filtered_out_results_list if filtered_out_results_list else [])} filtered_out results.")
        except (TypeError, ValueError) as e:
            logging.error(
                f"select_media: CRITICAL - Failed to unpack results from process_media_selection. Expected 2-tuple. Got type: {type(result_tuple_or_error)}. Value (first 500 chars): {str(result_tuple_or_error)[:500]}... Exception: {e}",
                exc_info=True
            )
            # This will be caught by the outer try/except and return a generic 500
            raise # Re-raise to ensure the function exits with an error
            
        # --- START DEBUGGING LOGS ---
        logging.info(f"select_media: PRE-JSONIFY check. Type of passed_results: {type(passed_results)}, Type of filtered_out_results_list: {type(filtered_out_results_list)}")
        if isinstance(passed_results, list):
            logging.info(f"select_media: PRE-JSONIFY passed_results is a list with {len(passed_results)} items. First item (if any): {str(passed_results[0]) if passed_results else 'Empty list'}")
        else:
            logging.info(f"select_media: PRE-JSONIFY passed_results is NOT a list. Value: {str(passed_results)[:500]}")

        if isinstance(filtered_out_results_list, list):
            logging.info(f"select_media: PRE-JSONIFY filtered_out_results_list is a list with {len(filtered_out_results_list)} items. First item (if any): {str(filtered_out_results_list[0]) if filtered_out_results_list else 'Empty list'}")
        else:
            logging.info(f"select_media: PRE-JSONIFY filtered_out_results_list is NOT a list. Value: {str(filtered_out_results_list)[:500]}")
        # --- END DEBUGGING LOGS ---

        # NZB season aggregate — run per-episode searches, filter+score, prepend virtual packs
        if multi and season and not episode and media_type in ('tv', 'show'):
            try:
                from scraper.newznab import scrape_newznab_season_aggregate
                from metadata.metadata import get_episode_count_for_seasons
                from scraper.functions.filter_results import filter_results as _filter_results
                from scraper.functions.rank_results import rank_result_key as _rank_result_key
                _all_scrapers = get_setting('Scrapers') or {}
                _nzb_scrapers = [
                    (sid, cfg) for sid, cfg in _all_scrapers.items()
                    if isinstance(cfg, dict) and cfg.get('type') == 'Newznab'
                    and cfg.get('enabled') and cfg.get('url') and cfg.get('api_key', '').strip()
                ]
                if _nzb_scrapers:
                    _imdb_id = session.get('last_selected_imdb_id') or ''
                    _ep_count = get_episode_count_for_seasons(_imdb_id, [season]) if _imdb_id else 0
                    if _ep_count > 0:
                        _virtual_packs = scrape_newznab_season_aggregate(
                            scrapers=_nzb_scrapers,
                            imdb_id=_imdb_id,
                            title=title,
                            year=int(year) if year else 0,
                            season=season,
                            episode_count=_ep_count,
                        )
                        if _virtual_packs:
                            # Load version settings for filter+score
                            _scraping_versions = get_setting('Scraping', 'versions', {})
                            _ver = (version or 'Default').strip('*')
                            _version_settings = _scraping_versions.get(_ver, {}) or {
                                'enable_hdr': True, 'max_resolution': '2160p', 'resolution_wanted': '<=',
                            }
                            _year_int = int(year) if year else 0
                            # Filter — title similarity, resolution, filter_in/out
                            _filtered, _filtered_out = _filter_results(
                                _virtual_packs, str(media_id), title, _year_int,
                                'episode', season, None, True, _version_settings,
                                runtime=0, episode_count=_ep_count,
                                season_episode_counts={season: _ep_count},
                                genres=genres,
                                imdb_id=_imdb_id,
                            )
                            # Don't add filtered-out NZB packs to filtered_out_results_list
                            # — they would appear as hidden "filtered" rows in the UI
                            if _filtered:
                                logging.info(f'[select_media] Adding {len(_filtered)} scored NZB virtual packs to results')
                                # Merge with normal results and sort together by score
                                _all = passed_results + _filtered
                                _all.sort(key=lambda x: _rank_result_key(
                                    x, _all, title, _year_int, season, None,
                                    True, 'episode', _version_settings,
                                ))
                                passed_results = _all
            except Exception as _agg_err:
                logging.warning(f'[select_media] NZB season aggregate failed: {_agg_err}', exc_info=True)

        # Return the results
        logging.debug(f"[select_media_route] Returning JSON for '{title}': passed_results={len(passed_results)}, filtered_out_results_list={len(filtered_out_results_list if filtered_out_results_list else [])}")
        return jsonify({
            'torrent_results': passed_results,
            'filtered_out_torrent_results': filtered_out_results_list 
        })
    except Exception as e:
        logging.error(f"Error in select_media: {str(e)}", exc_info=True)
        return jsonify({'error': 'An error occurred while processing your request'}), 500

def _get_content_source_display_name(content_source_id):
    """Resolve content source ID to its display_name from settings config."""
    if not content_source_id:
        return None
    try:
        from utilities.settings import get_setting as _gs2
        sources = _gs2('Content Sources', None, {})
        if not sources:
            from utilities.settings import load_config as _lc
            cfg = _lc()
            sources = cfg.get('Content Sources', {})
        source_cfg = sources.get(content_source_id, {})
        return source_cfg.get('display_name', '').strip() or None
    except Exception:
        return None


def _build_nzb_title(title, year, imdb_id, version, original_scraped_torrent_title,
                     media_type=None, season=None, episode=None, episode_title=None,
                     pack_original=None, tags=None, content_source_display_name=None):
    """
    Build a structured NZB job title when 'Enable NZB File Naming' is on.
    This becomes both the cli_mount folder name and filename.
    Falls back to original_scraped_torrent_title if setting is off or data missing.

    pack_original: if provided, used for the (original) bracket instead of
                   original_scraped_torrent_title. Used by NZB aggregate packs so
                   the bracket always shows the pack quality tags regardless of
                   per-episode filename availability.
    """
    from utilities.settings import get_setting as _gs
    if not _gs('Usenet Provider', 'enable_nzb_naming', False):
        return original_scraped_torrent_title

    import re as _re
    # Sanitize components for use in filenames
    def _san(s):
        return _re.sub(r'[\\/*?:"<>|]', '', str(s or '')).strip()

    _title = _san(title)
    _year = _san(year)
    _imdb = _san(imdb_id) if imdb_id else ''
    # Version toggle
    _include_version = _gs('Usenet Provider', 'include_version_in_nzb_naming', True)
    _version = _san(version).strip('*') if (version and _include_version) else ''
    # Content source toggle
    _include_cs = _gs('Usenet Provider', 'include_content_source_in_nzb_naming', False)
    _cs_part = _san(content_source_display_name) if (_include_cs and content_source_display_name) else ''
    # (original) bracket uses pack_original when provided, else original_scraped_torrent_title
    _orig_full = _san(pack_original or original_scraped_torrent_title or '')
    # Strip redundant prefix — pass 1: year-based, pass 2: resolution-based for episodes.
    _orig = _orig_full
    if _orig_full and _year:
        _strip_pat = _re.compile(
            r'^.*?[\s.]' + _re.escape(str(_year)) + r'[\s.]+',
            _re.IGNORECASE
        )
        _stripped = _strip_pat.sub('', _orig_full).strip(' .')
        if _stripped and _stripped != _orig_full:
            _orig = _stripped
    if _orig == _orig_full and _orig_full:
        _ep_strip = _re.compile(r'^.*?((?:2160|1080|720)[pP][\s.])', _re.IGNORECASE)
        _em = _ep_strip.match(_orig_full)
        if _em:
            _orig = _orig_full[_em.start(1):].strip(' .')
    # Tags are no longer embedded in the title — they're pushed to cli_mount
    # directly via CliMountClient.push_tags() instead. Kept as a no-op var so
    # the _assemble(...) call sites below don't need to change.
    _tags_part = ''

    is_episode = media_type in ('tv', 'show', 'episode') and season is not None and episode is not None

    # NZB name is used as both folder AND filename: folder/folder.nzb
    # Full path = folder + '/' + folder + '.nzb' = 2*folder + 5
    # Keep full path <= 240: folder <= (240-5)/2 = 117
    _MAX_TITLE = 117

    def _assemble(*p):
        return ' - '.join(x for x in p if x)

    if is_episode:
        _ep_title = _san(episode_title or '')
        base = _assemble(f'{_title} ({_year})', f'S{int(season):02d}E{int(episode):02d}')
        _imdb_part = f'{{imdb-{_imdb}}}' if _imdb else ''

        # Try full title first: base - ep_title - imdb - tags - version - cs - (orig)
        full = _assemble(base, _ep_title, _imdb_part, _tags_part, _version, _cs_part, f'({_orig})' if _orig else '')
        if len(full) <= _MAX_TITLE:
            return full

        # Drop episode title
        without_ep = _assemble(base, _imdb_part, _tags_part, _version, _cs_part, f'({_orig})' if _orig else '')
        if len(without_ep) <= _MAX_TITLE:
            return without_ep

        # Drop content source
        without_cs = _assemble(base, _imdb_part, _tags_part, _version, f'({_orig})' if _orig else '')
        if len(without_cs) <= _MAX_TITLE:
            return without_cs

        # Truncate (original) to fit — always keep it, just shorter. Drop
        # tags/version from fixed_part first if even that alone leaves no room,
        # so imdb_part (the field cli_debrid uses to re-match this entry later)
        # is the last thing ever dropped, never the first.
        if _orig:
            for fixed_part in (
                _assemble(base, _imdb_part, _tags_part, _version),
                _assemble(base, _imdb_part, _version),
                _assemble(base, _imdb_part),
            ):
                # " - (" prefix (4 chars) + ")" suffix (1 char) = 5 chars overhead
                available = _MAX_TITLE - len(fixed_part) - 5
                if available > 10:
                    truncated_orig = _orig[:available]
                    return f'{fixed_part} - ({truncated_orig})'

        # Last resort: no (original), but imdb is still present.
        without_orig = _assemble(base, _imdb_part)
        if len(without_orig) <= _MAX_TITLE:
            return without_orig
        return base[:_MAX_TITLE]
    else:
        _season_part = f'S{int(season):02d}' if season is not None else ''
        base = _assemble(f'{_title} ({_year})', _season_part)
        _imdb_str = f'{{imdb-{_imdb}}}' if _imdb else ''
        _orig_part = f'({_orig})' if _orig else ''

        # imdb_id must never be the thing dropped to fit _MAX_TITLE — it is the
        # only field cli_debrid uses to re-match this entry later (see
        # repair_engine.py Strategy 4). Drop content-source, tags, then version
        # first; only then truncate — and finally drop — the (original) bracket,
        # which is decorative. This mirrors the episode branch above, which
        # already protects imdb_part the same way.
        for attempt in [
            _assemble(base, _imdb_str, _tags_part, _version, _cs_part, _orig_part),
            _assemble(base, _imdb_str, _tags_part, _version, _orig_part),
            _assemble(base, _imdb_str, _version, _orig_part),
            _assemble(base, _imdb_str, _orig_part),
        ]:
            if len(attempt) <= _MAX_TITLE:
                return attempt

        # Truncate (original) to fit — always keep it, just shorter.
        if _orig:
            fixed_part = _assemble(base, _imdb_str)
            # " - (" prefix (4 chars) + ")" suffix (1 char) = 5 chars overhead
            available = _MAX_TITLE - len(fixed_part) - 5
            if available > 10:
                truncated_orig = _orig[:available]
                return f'{fixed_part} - ({truncated_orig})'

        # Last resort: no (original), but imdb is still present.
        without_orig = _assemble(base, _imdb_str)
        if len(without_orig) <= _MAX_TITLE:
            return without_orig
        return base[:_MAX_TITLE]


def _build_debrid_title(title, year, imdb_id, version, original_scraped_torrent_title,
                        media_type=None, season=None, episode=None, episode_title=None,
                        tags=None, content_source_display_name=None):
    """
    Build a structured debrid folder name when 'Enable Debrid File Naming' is on.
    This becomes the cli_mount DFS mount folder/file name for the debrid torrent.
    Completely separate from _build_nzb_title — reads Debrid Provider settings only.
    Falls back to original_scraped_torrent_title if setting is off or data missing.
    Tags are pushed to cli_mount directly via CliMountClient.push_tags(), not embedded here.
    """
    from utilities.settings import get_setting as _gs
    if not _gs('Debrid Provider', 'enable_debrid_naming', False):
        return original_scraped_torrent_title

    import re as _re

    def _san(s):
        return _re.sub(r'[\\/*?:"<>|]', '', str(s or '')).strip()

    _title = _san(title)
    _year = _san(year)
    _imdb = _san(imdb_id) if imdb_id else ''
    _include_version = _gs('Debrid Provider', 'include_version_in_debrid_naming', True)
    _version = _san(version).strip('*') if (version and _include_version) else ''
    _include_cs = _gs('Debrid Provider', 'include_content_source_in_debrid_naming', False)
    _cs_part = _san(content_source_display_name) if (_include_cs and content_source_display_name) else ''
    _orig_full = _san(original_scraped_torrent_title or '')
    # Strip redundant prefix from the original torrent name so only quality tags remain.
    # Pass 1: strip everything up to and including the year (movies/shows with year).
    # Pass 2: strip SxxExx + optional episode title (episode filenames without year).
    _orig = _orig_full
    if _orig_full and _year:
        _strip_pat = _re.compile(
            r'^.*?[\s.]' + _re.escape(str(_year)) + r'[\s.]+',
            _re.IGNORECASE
        )
        _stripped = _strip_pat.sub('', _orig_full).strip(' .')
        if _stripped and _stripped != _orig_full:
            _orig = _stripped
    if _orig == _orig_full and _orig_full:
        # No year found — try stripping everything up to resolution/quality tag.
        # Handles episode filenames like "Show S10E04 Title 1080p TrueHD..." → "1080p TrueHD..."
        _ep_strip = _re.compile(r'^.*?((?:2160|1080|720)[pP][\s.])', _re.IGNORECASE)
        _em = _ep_strip.match(_orig_full)
        if _em:
            _orig = _orig_full[_em.start(1):].strip(' .')
    # Tags are no longer embedded in the title — they're pushed to cli_mount
    # directly via CliMountClient.push_tags() instead. Kept as a no-op var so
    # the _assemble(...) call sites below don't need to change.
    _tags_part = ''

    is_episode = media_type in ('tv', 'show', 'episode') and season is not None and episode is not None

    # Debrid folder name is used as both folder AND filename: folder/folder.mkv
    # Full path = folder + '/' + folder + '.mkv' = 2*folder + 5
    # Keep full path <= 240: folder <= (240-5)/2 = 117
    _MAX_TITLE = 117

    def _assemble(*p):
        return ' - '.join(x for x in p if x)

    # (original) is mandatory — drop cs/version/tags before dropping it
    _orig_part = f'({_orig})' if _orig else ''

    if is_episode:
        _ep_title = _san(episode_title or '')
        base = _assemble(f'{_title} ({_year})', f'S{int(season):02d}E{int(episode):02d}')
        _imdb_part = f'{{imdb-{_imdb}}}' if _imdb else ''

        # imdb_id must never be the thing dropped to fit _MAX_TITLE — it is the
        # only field cli_debrid uses to re-match this entry later (see
        # repair_engine.py Strategy 4). Drop ep_title/cs/tags/version first;
        # only then truncate — and finally drop — the (original) bracket,
        # which is decorative.
        for attempt in [
            _assemble(base, _ep_title, _imdb_part, _tags_part, _version, _cs_part, _orig_part),
            _assemble(base, _imdb_part, _tags_part, _version, _cs_part, _orig_part),
            _assemble(base, _imdb_part, _tags_part, _version, _orig_part),
            _assemble(base, _imdb_part, _orig_part),
        ]:
            if len(attempt) <= _MAX_TITLE:
                return attempt

        # Truncate (original) to fit — always keep it, just shorter.
        if _orig:
            fixed_part = _assemble(base, _imdb_part)
            # " - (" prefix (4 chars) + ")" suffix (1 char) = 5 chars overhead
            available = _MAX_TITLE - len(fixed_part) - 5
            if available > 10:
                truncated_orig = _orig[:available]
                return f'{fixed_part} - ({truncated_orig})'

        # Last resort: no (original), but imdb is still present.
        without_orig = _assemble(base, _imdb_part)
        if len(without_orig) <= _MAX_TITLE:
            return without_orig
        return base[:_MAX_TITLE]
    else:
        _season_part = f'S{int(season):02d}' if season is not None else ''
        base = _assemble(f'{_title} ({_year})', _season_part)
        _imdb_str = f'{{imdb-{_imdb}}}' if _imdb else ''

        # imdb_id must never be the thing dropped to fit _MAX_TITLE — it is the
        # only field cli_debrid uses to re-match this entry later (see
        # repair_engine.py Strategy 4). Drop content-source and tags first;
        # only then truncate — and finally drop — the (original) bracket,
        # which is decorative.
        for attempt in [
            _assemble(base, _imdb_str, _tags_part, _version, _cs_part, _orig_part),
            _assemble(base, _imdb_str, _tags_part, _version, _orig_part),
            _assemble(base, _imdb_str, _version, _orig_part),
            _assemble(base, _imdb_str, _orig_part),
        ]:
            if len(attempt) <= _MAX_TITLE:
                return attempt

        # Truncate (original) to fit — always keep it, just shorter.
        if _orig:
            fixed_part = _assemble(base, _imdb_str)
            # " - (" prefix (4 chars) + ")" suffix (1 char) = 5 chars overhead
            available = _MAX_TITLE - len(fixed_part) - 5
            if available > 10:
                truncated_orig = _orig[:available]
                return f'{fixed_part} - ({truncated_orig})'

        # Last resort: no (original), but imdb is still present.
        without_orig = _assemble(base, _imdb_str)
        if len(without_orig) <= _MAX_TITLE:
            return without_orig
        return base[:_MAX_TITLE]


def _submit_single_episode_nzb(client, nzb_url, ep_label, is_anime=False, media_type='',
                                tags=None, tags_exclusive=False):
    """Fetch + submit one episode NZB.
    Returns (job_id, nzb_text, missing_segments) where missing_segments=True means
    cli_mount's server couldn't find the articles (abort pack, don't blacklist)."""
    from routes.api_tracker import api as _nzb_api
    from database.not_wanted_magnets import is_nzb_segment_not_wanted
    nzb_text = None
    try:
        r = _nzb_api.get(nzb_url, timeout=15, allow_redirects=True)
        if r.status_code != 200 or '<nzb' not in r.text.lower():
            logging.warning(f'[NZBPack] {ep_label}: bad NZB response (status={r.status_code})')
            return None, None, False
        nzb_text = r.text
        if is_nzb_segment_not_wanted(nzb_text):
            logging.info(f'[NZBPack] {ep_label}: segment in not-wanted list, skipping')
            return None, None, False
        job_id = client.add_nzb_content(nzb_content=nzb_text, title=ep_label,
                                        is_anime=is_anime, media_type=media_type,
                                        tags=tags, tags_exclusive=tags_exclusive)
        if not job_id and client.last_missing_segments:
            logging.warning(f'[NZBPack] {ep_label}: cli_mount server missing segments — aborting pack (NZB not blacklisted)')
            return None, None, True
        return job_id, nzb_text, False
    except Exception as e:
        logging.warning(f'[NZBPack] {ep_label}: submit error: {e}')
        return None, None, False


def _add_nzb_pack_to_usenet(episode_nzb_urls, fallback_nzb_urls, title, year, media_type,
                             season, version, tmdb_id, original_scraped_torrent_title=None,
                             episode_filenames=None,
                             genres=None, current_score=0.0,
                             selected_folder=None, selected_folder_is_custom=False,
                             selected_tags=None,
                             existing_items=None):
    """Submit a virtual NZB season pack — one NZB per episode with health-check + retry.

    existing_items: optional dict {ep_num: item_dict} of existing DB items to reuse instead of
                    creating new ones. When provided, those items are updated in-place (state →
                    Adding) preserving their IDs and history. Used by scraping_queue batch path.
    """
    from usenet.climount_client import get_climount_client, reset_climount_client
    from database.not_wanted_magnets import add_to_not_wanted_nzb_segment, extract_nzb_segment_id
    from metadata.metadata import get_metadata, get_release_date
    from database.database_writing import add_media_item, update_media_item_state, update_media_item

    reset_climount_client()
    client = get_climount_client()
    if not client.is_enabled():
        return jsonify({'error': 'Usenet provider (cli_mount) is not enabled.'}), 503

    # Resolve metadata once
    imdb_id = None
    episode_titles = {}
    episode_imdb_ids = {}
    try:
        meta = get_metadata(tmdb_id=int(tmdb_id), item_media_type=media_type)
        imdb_id = meta.get('imdb_id')
        # Fallback: resolve imdb_id directly if get_metadata didn't return it
        if not imdb_id and tmdb_id:
            try:
                from metadata.metadata import get_imdb_id_if_missing
                _api_type = 'show' if media_type in ('tv', 'show') else media_type
                imdb_id = get_imdb_id_if_missing({'tmdb_id': int(tmdb_id), 'media_type': _api_type})
            except Exception as _ie:
                logging.warning(f'[NZBPack] imdb_id fallback failed: {_ie}')
        # Episode titles and IMDb IDs via DirectAPI (has full season data)
        if imdb_id:
            try:
                from metadata.metadata import DirectAPI
                show_meta, _ = DirectAPI.get_show_metadata(imdb_id)
                season_data = (show_meta.get('seasons') or {}).get(str(season), {}) or \
                              (show_meta.get('seasons') or {}).get(season, {})
                episode_titles = {int(k): v.get('title', f'Episode {k}')
                                 for k, v in (season_data.get('episodes') or {}).items()}
                episode_imdb_ids = {int(k): v['imdb_id']
                                    for k, v in (season_data.get('episodes') or {}).items()
                                    if v.get('imdb_id')}
            except Exception:
                pass
    except Exception as _me:
        logging.warning(f'[NZBPack] Metadata fetch failed: {_me}')

    if not imdb_id:
        logging.warning(f'[NZBPack] Could not resolve imdb_id for tmdb_id={tmdb_id} — replace cleanup will not fire')

    # Pre-load existing cli_mount NZB names to skip already-submitted episodes
    _existing_nzb_names = set()
    try:
        from routes.api_tracker import api as _dcy_check_api
        from utilities.settings import get_setting as _gs2
        _dcy_url2 = _gs2('Usenet Provider', 'url', default='').rstrip('/')
        _dcy_token2 = _gs2('Usenet Provider', 'api_token', default='')
        _dh2 = {'Authorization': f'Bearer {_dcy_token2}'} if _dcy_token2 else {}
        _pg2 = 1
        while True:
            _tr2 = _dcy_check_api.get(f'{_dcy_url2}/api/browse/nzbs',
                                       params={'page': _pg2, 'limit': 100},
                                       headers=_dh2, timeout=10)
            if _tr2.status_code != 200:
                break
            _td2 = _tr2.json()
            _entries2 = _td2.get('entries', _td2) if isinstance(_td2, dict) else _td2
            if not _entries2:
                break
            for _n2 in _entries2:
                _name2 = _n2.get('name') or _n2.get('title') or _n2.get('filename') or ''
                if _name2:
                    _existing_nzb_names.add(_name2.strip())
            _total2 = _td2.get('total_pages', 1) if isinstance(_td2, dict) else 1
            if _pg2 >= _total2:
                break
            _pg2 += 1
        logging.info(f'[NZBPack] Loaded {len(_existing_nzb_names)} existing cli_mount NZBs for dedup check')
    except Exception as _de2:
        logging.warning(f'[NZBPack] Could not load existing cli_mount NZBs: {_de2}')

    # Pre-load already-collected episodes to avoid duplicates
    # Exclude episodes marked manual_replace=1 — those are intentional replacements
    _collected_eps = set()
    if imdb_id:
        try:
            from database import get_db_connection as _get_db
            _conn = _get_db()
            _rows = _conn.execute(
                "SELECT episode_number FROM media_items WHERE imdb_id=? AND season_number=? AND type='episode' AND state IN ('Collected','Upgrading') AND (manual_replace IS NULL OR manual_replace=0)",
                (imdb_id, season)
            ).fetchall()
            _collected_eps = {r[0] for r in _rows}
            _conn.close()
            if _collected_eps:
                logging.info(f'[NZBPack] Skipping already-collected episodes (no replace flag): {sorted(_collected_eps)}')
        except Exception as _ce:
            logging.warning(f'[NZBPack] Could not check collected episodes: {_ce}')

    submitted = []
    failed_eps = []
    pack_expired = False  # set True when missing segments detected — abort remaining episodes

    for ep_num in sorted(episode_nzb_urls.keys()):
        if pack_expired:
            failed_eps.append(ep_num)
            continue

        if ep_num in _collected_eps:
            logging.info(f'[NZBPack] Skipping S{season:02d}E{ep_num:02d} — already Collected')
            continue

        import re as _re_label
        # Aggregate pack display title — always has quality tags, used for (original) bracket
        _pack_orig = _re_label.sub(r'\s*\[NZB Pack[^\]]*\]', '', original_scraped_torrent_title or title).strip()

        # Per-episode raw filename — used only for the cli_mount job name (has SxxExx)
        _ep_raw = (episode_filenames or {}).get(ep_num, '') or ''
        _ep_raw_clean = _re_label.sub(r'\.(mkv|mp4|avi|m4v|nfo|nzb)$', '', _ep_raw, flags=_re_label.IGNORECASE).strip()
        if not _ep_raw_clean:
            _ep_raw_clean = _pack_orig  # fallback job name still has quality tags

        ep_label = _build_nzb_title(
            title=title, year=year, imdb_id=imdb_id,
            version=version, original_scraped_torrent_title=f'{_ep_raw_clean}-[NZB Pack]',
            media_type='episode', season=season, episode=ep_num,
            episode_title=episode_titles.get(ep_num),
            pack_original=f'{_pack_orig}-[NZB Pack]',
            tags=selected_tags or None,
        ) or f'{_ep_raw_clean}-[NZB Pack]'
        primary_url = episode_nzb_urls[ep_num]
        fallbacks = fallback_nzb_urls.get(ep_num, [])

        job_id = None
        nzb_text = None

        _is_anime = any('anime' in (g or '').lower() for g in (genres or []))
        # Try primary then fallbacks
        for url in [primary_url] + fallbacks:
            if not url:
                continue
            job_id, nzb_text, expired = _submit_single_episode_nzb(
                client, url, ep_label, is_anime=_is_anime, media_type='episode',
                tags=selected_tags, tags_exclusive=False)
            if expired:
                # Missing segments on this URL — no point trying more fallbacks from same release
                pack_expired = True
                break
            if not job_id:
                continue

            # Health check
            try:
                health = client.check_entry_health(ep_label)
                if health == 'broken':
                    logging.warning(f'[NZBPack] {ep_label}: broken — removing and trying next')
                    # Add to not-wanted (guid-based, works at scrape time)
                    try:
                        from database.not_wanted_magnets import add_to_not_wanted_nzb_guid as _add_guid
                        _add_guid(primary_url)
                    except Exception:
                        pass
                    # Delete from cli_mount
                    try:
                        from routes.api_tracker import api as _del_api
                        from utilities.settings import get_setting as _gs
                        _dcy_url = _gs('Usenet Provider', 'url', default='').rstrip('/')
                        _dcy_token = _gs('Usenet Provider', 'api_token', default='')
                        _dh = {'Authorization': f'Bearer {_dcy_token}'} if _dcy_token else {}
                        _pg = 1
                        _real_hash = None
                        while not _real_hash:
                            _tr = _del_api.get(f'{_dcy_url}/api/torrents',
                                               params={'page': _pg, 'limit': 50},
                                               headers=_dh, timeout=10)
                            if _tr.status_code != 200:
                                break
                            _td = _tr.json()
                            for _t in _td.get('torrents', []):
                                if _t.get('name', '').strip() == ep_label:
                                    _real_hash = _t.get('info_hash', '')
                                    break
                            if _real_hash or not _td.get('has_next'):
                                break
                            _pg += 1
                        if _real_hash:
                            _del_api.delete(f'{_dcy_url}/api/torrents',
                                            headers=_dh, params={'hashes': _real_hash}, timeout=10)
                    except Exception as _de:
                        logging.warning(f'[NZBPack] Could not delete broken entry: {_de}')
                    job_id = None
                    nzb_text = None
                    continue  # try next fallback
                else:
                    logging.info(f'[NZBPack] {ep_label}: health={health or "inconclusive"} — keeping')
            except Exception as _he:
                logging.debug(f'[NZBPack] {ep_label}: health check error: {_he} — keeping')

            break  # submitted and passed (or inconclusive)

        if job_id:
            submitted.append((ep_num, job_id))
            # Create queue item for this episode
            try:
                checking_id = f"nzb:{job_id}" if not str(job_id).startswith('nzb:') else str(job_id)
                # ep_label is what was submitted to cli_mount — adding_queue uses
                # filled_by_file to look up the entry, so it must match exactly
                # Reuse existing item if provided (scraping_queue batch path)
                # otherwise create a new item (scraper UI path)
                existing_item = (existing_items or {}).get(ep_num)
                if existing_item:
                    item_id = existing_item['id']
                    update_media_item_state(item_id, 'Adding')
                else:
                    ep_item = {
                        'title': title, 'year': year, 'type': 'episode',
                        'version': version, 'tmdb_id': tmdb_id, 'imdb_id': imdb_id,
                        'season_number': season, 'episode_number': ep_num,
                        'episode_title': episode_titles.get(ep_num, f'Episode {ep_num}'),
                        'release_date': 'Unknown',
                        'genres': json.dumps(genres or []),
                        'current_score': current_score,
                        'original_scraped_torrent_title': ep_label,
                        'content_source': 'content_requester',
                        'selected_folder': selected_folder,
                        'selected_folder_is_custom': selected_folder_is_custom,
                        'tags': selected_tags,
                    }
                    item_id = add_media_item(ep_item)
                if item_id:
                    update_media_item_state(item_id, 'Adding')
                    _ep_seg_id = ''
                    if nzb_text:
                        try:
                            from database.not_wanted_magnets import extract_nzb_segment_id as _ext_seg
                            _ep_seg_id = _ext_seg(nzb_text)
                        except Exception:
                            pass
                    _ep_seg_kwargs = {'nzb_segment_id': _ep_seg_id} if _ep_seg_id else {}
                    update_media_item(item_id,
                        filled_by_torrent_id=checking_id,
                        filled_by_file=ep_label,
                        filled_by_magnet=primary_url,
                        original_scraped_torrent_title=ep_label,
                        **_ep_seg_kwargs,
                    )
            except Exception as _qe:
                logging.warning(f'[NZBPack] Queue tracking failed for {ep_label}: {_qe}')
        else:
            failed_eps.append(ep_num)
            logging.warning(f'[NZBPack] {ep_label}: all URLs exhausted — episode will need normal queue fill')

    ep_total = len(episode_nzb_urls)
    if pack_expired:
        msg = f'Pack aborted — missing segments detected (Usenet retention exceeded). {len(submitted)}/{ep_total} episodes submitted before abort.'
    else:
        msg = f'{len(submitted)}/{ep_total} episodes submitted to {_usenet_pname()}.'
    if failed_eps and not pack_expired:
        msg += f' Episodes {failed_eps} not found — will be filled by normal queue.'

    logging.info(f'[NZBPack] {title} S{season:02d}: {msg}')
    return jsonify({
        'success': True,
        'message': msg,
        'submitted': len(submitted),
        'failed_episodes': failed_eps,
        'provider': 'cli_mount',
    })


def _add_nzb_to_usenet(nzb_url, title, year, media_type, season, episode, version, tmdb_id,
                       original_scraped_torrent_title=None, genres=None, current_score=0.0,
                       selected_folder=None, selected_folder_is_custom=False, selected_tags=None):
    """Submit an NZB URL to cli_mount and track it through the queue like a debrid add."""
    from usenet.climount_client import get_climount_client, reset_climount_client
    from metadata.metadata import get_metadata, get_release_date
    reset_climount_client()
    client = get_climount_client()

    if not client.is_enabled():
        return jsonify({'error': 'Usenet provider (cli_mount) is not enabled. Configure it in Required Settings.'}), 503

    if not nzb_url:
        return jsonify({'error': 'No NZB URL provided'}), 400

    # Pre-fetch NZB XML to check not-wanted list before submitting
    from routes.api_tracker import api as _nzb_api
    from database.not_wanted_magnets import is_nzb_segment_not_wanted as _is_not_wanted
    _nzb_xml = None
    try:
        _nr = _nzb_api.get(nzb_url, timeout=15, allow_redirects=True)
        if _nr.status_code == 200 and '<nzb' in _nr.text.lower():
            _nzb_xml = _nr.text
    except Exception:
        pass

    if _nzb_xml and _is_not_wanted(_nzb_xml):
        logging.info(f'[NZB] Skipping {title!r} — segment ID in not-wanted list (previously broken)')
        return jsonify({'error': f'This NZB is known broken and has been blacklisted: {title}'}), 400

    logging.info(f'[NZB] Submitting to {_usenet_pname()}: {title} ({year})')

    # Build job title — uses structured naming template when enabled
    _imdb_id_for_title = None
    _ep_title = None
    if tmdb_id:
        try:
            _title_meta = get_metadata(tmdb_id=int(tmdb_id), item_media_type=media_type)
            _imdb_id_for_title = _title_meta.get('imdb_id')
            if media_type in ('tv', 'show') and season is not None and episode is not None:
                _s = ((_title_meta.get('seasons') or {}).get(season) or
                      (_title_meta.get('seasons') or {}).get(str(season)) or {})
                _ep_title = ((_s.get('episodes') or {}).get(episode) or
                             (_s.get('episodes') or {}).get(str(episode)) or {}).get('title')
        except Exception:
            pass
    _job_title = _build_nzb_title(
        title=title, year=year, imdb_id=_imdb_id_for_title,
        version=version, original_scraped_torrent_title=original_scraped_torrent_title,
        media_type=media_type, season=season, episode=episode, episode_title=_ep_title,
        tags=selected_tags or None,
    )

    _is_anime = any('anime' in (g or '').lower() for g in (genres or []))
    _submit_title = str(_job_title or title or '')
    # Submit — use pre-fetched content directly if available to avoid double-fetch
    if _nzb_xml:
        job_id = client.add_nzb_content(nzb_content=_nzb_xml, title=_submit_title,
                                        is_anime=_is_anime, media_type=media_type or '',
                                        tags=selected_tags, tags_exclusive=False)
        if not job_id and client.last_missing_segments:
            logging.warning(f'[NZB] cli_mount server missing segments for {title!r}')
        elif job_id:
            logging.info(f'[NZB] Submitted via content upload: {title}')
    else:
        job_id = client.add_nzb(nzb_url=nzb_url, title=_submit_title,
                                is_anime=_is_anime, media_type=media_type or '',
                                tags=selected_tags, tags_exclusive=False)

    if not job_id:
        return jsonify({'error': 'Failed to submit NZB to cli_mount'}), 500

    logging.info(f'[NZB] Submitted successfully, job_id={job_id}')

    # --- Queue tracking: create media item and move through queue like debrid ---
    try:
        nzb_title = _job_title or original_scraped_torrent_title or title or ''
        checking_id = f"nzb:{job_id}" if not str(job_id).startswith('nzb:') else str(job_id)

        # Get metadata
        imdb_id = None
        release_date = 'Unknown'
        if tmdb_id:
            try:
                meta = get_metadata(tmdb_id=int(tmdb_id), item_media_type=media_type)
                imdb_id = meta.get('imdb_id')
                if media_type not in ['tv', 'show']:
                    release_date = get_release_date(meta, imdb_id) or 'Unknown'
            except Exception as _me:
                logging.warning(f'[NZB] Could not fetch metadata for queue tracking: {_me}')

        item_type = 'episode' if media_type in ['tv', 'show'] else 'movie'
        resolution = extract_resolution_from_filename(nzb_title) if nzb_title else None

        base_item = {
            'title': title,
            'year': year,
            'type': item_type,
            'version': version,
            'resolution': resolution,
            'tmdb_id': tmdb_id,
            'imdb_id': imdb_id,
            'release_date': release_date,
            'genres': json.dumps(genres or []),
            'current_score': current_score,
            'original_scraped_torrent_title': nzb_title,
            'content_source': 'content_requester',
            'selected_folder': selected_folder,
            'selected_folder_is_custom': selected_folder_is_custom,
            'tags': selected_tags,
        }

        from queues.queue_manager import QueueManager
        from database.database_writing import add_media_item
        queue_manager = QueueManager()

        from database.database_reading import get_media_item_by_id
        from database.database_writing import update_media_item_state, update_media_item

        # Extract segment ID from NZB XML already fetched above (zero extra HTTP call)
        _nzb_seg_id = ''
        if _nzb_xml:
            try:
                from database.not_wanted_magnets import extract_nzb_segment_id as _ext_seg2
                _nzb_seg_id = _ext_seg2(_nzb_xml)
            except Exception:
                pass

        def _place_nzb_in_adding(item_id_to_place):
            """Put a freshly-created item into Adding state with NZB fields set for health-check polling."""
            update_media_item_state(item_id_to_place, 'Adding')
            _place_seg_kwargs = {'nzb_segment_id': _nzb_seg_id} if _nzb_seg_id else {}
            update_media_item(item_id_to_place,
                filled_by_torrent_id=checking_id,
                filled_by_file=nzb_title,
                filled_by_magnet=nzb_url,
                original_scraped_torrent_title=nzb_title,
                **_place_seg_kwargs,
            )
            logging.info(f'[NZB] Item {item_id_to_place} placed in Adding queue for health check (checking_id={checking_id})')

        if item_type == 'episode' and season is not None and episode is None:
            # Season pack — create items per episode
            try:
                meta = get_metadata(tmdb_id=int(tmdb_id), item_media_type=media_type)
                season_data = (meta.get('seasons') or {}).get(season, {})
                episodes = season_data.get('episodes', {})
                for ep_num_str, ep_data in episodes.items():
                    ep_item = base_item.copy()
                    ep_item.update({'season_number': season, 'episode_number': int(ep_num_str),
                                    'episode_title': ep_data.get('title', f'Episode {ep_num_str}')})
                    item_id = add_media_item(ep_item)
                    if item_id:
                        _place_nzb_in_adding(item_id)
            except Exception as _spe:
                logging.warning(f'[NZB] Season pack queue tracking failed: {_spe}')
        else:
            if item_type == 'episode':
                base_item.update({'season_number': season, 'episode_number': episode})
            item_id = add_media_item(base_item)
            if item_id:
                _place_nzb_in_adding(item_id)
    except Exception as _qe:
        logging.error(f'[NZB] Queue tracking failed for {title}: {_qe}', exc_info=True)
        # Don't fail the response — NZB was submitted successfully to cli_mount

    return jsonify({
        'success': True,
        'message': f'NZB submitted to {_usenet_pname()} (job: {job_id}). Tracking through queue.',
        'job_id': job_id,
        'provider': 'cli_mount',
    })


@scraper_bp.route('/add_torrent', methods=['POST'])
@user_required
@scraper_permission_required
def add_torrent():
    torrent_index = int(request.form.get('torrent_index'))
    torrent_results = session.get('torrent_results', [])
    
    if 0 <= torrent_index < len(torrent_results):
        result = process_torrent_selection(torrent_index, torrent_results)
        if result['success']:
            return render_template('scraper.html', success_message=result['message'])
        else:
            return render_template('scraper.html', error=result['error'])
    else:
        return render_template('scraper.html', error="Invalid torrent selection")
    
@scraper_bp.route('/api/search', methods=['GET'])
@user_required
def api_battery_search():
    """Search using battery DirectAPI — same source the scraper uses internally."""
    from cli_battery.app.direct_api import DirectAPI
    query = request.args.get('query', '').strip()
    if not query:
        return jsonify({'results': [], 'error': 'No query provided'}), 400
    try:
        results, source = DirectAPI.search_media(query)
        if not results:
            return jsonify({'results': []})
        normalized = []
        for r in results:
            normalized.append({
                'id': r.get('tmdb_id'),
                'tmdb_id': r.get('tmdb_id'),
                'imdb_id': r.get('imdb_id'),
                'title': r.get('title'),
                'year': r.get('year'),
                'media_type': 'tv' if r.get('type') == 'show' else r.get('type', 'movie'),
            })
        return jsonify({'results': normalized, 'source': source})
    except Exception as e:
        log.error(f"Battery search error: {e}", exc_info=True)
        return jsonify({'results': [], 'error': str(e)}), 500


@scraper_bp.route('/scraper_tester', methods=['GET', 'POST'])
@admin_required
@onboarding_required
def scraper_tester():
    if request.method == 'POST':
        if request.is_json:
            data = request.json
            search_term = data.get('search_term')
        else:
            search_term = request.form.get('search_term')
        
        if search_term:
            # Use the parse_search_term function from web_scraper
            from utilities.web_scraper import parse_search_term
            base_title, season, episode, year, multi = parse_search_term(search_term)
            
            # Use the parsed title and year for search
            search_results = search_trakt(base_title, year)
            
            # Fetch IMDB IDs and season/episode counts for each result
            for result in search_results:
                details = get_details(result)
                
                if details:
                    imdb_id = details.get('externalIds', {}).get('imdbId', 'N/A')
                    tmdb_id = details.get('id', 'N/A')
                    result['imdbId'] = imdb_id
                    
                    if result['mediaType'] == 'tv':
                        season_episode_counts = get_all_season_episode_counts(tmdb_id)
                        result['seasonEpisodeCounts'] = season_episode_counts
                else:
                    result['imdbId'] = 'N/A'
            
            return jsonify(search_results)
        else:
            return jsonify({'error': 'No search term provided'}), 400
    
    # GET request handling
    all_settings = get_all_settings()
    versions = all_settings.get('Scraping', {}).get('versions', {}).keys()
        
    return render_template('scraper_tester.html', versions=versions)

@scraper_bp.route('/get_item_details', methods=['POST'])
@user_required
def get_item_details():
    from metadata.metadata import get_metadata, get_release_date
    item = request.json
    details = get_details(item)
    
    if details:
        # Ensure IMDB ID is included
        imdb_id = details.get('externalIds', {}).get('imdbId', '')
        
        response_data = {
            'imdb_id': imdb_id,
            'tmdb_id': str(details.get('id', '')),
            'title': details.get('title') if item['mediaType'] == 'movie' else details.get('name', ''),
            'year': details.get('releaseDate', '')[:4] if item['mediaType'] == 'movie' else details.get('firstAirDate', '')[:4],
            'mediaType': item['mediaType']
        }
        return jsonify(response_data)
    else:
        return jsonify({'error': 'Could not fetch details'}), 400

@scraper_bp.route('/get_media_meta', methods=['POST'])
@user_required
def get_media_meta_endpoint():
    from metadata.metadata import get_metadata
    from utilities.web_scraper import get_media_meta
    data = request.json
    tmdb_id = data.get('tmdb_id')
    media_type = data.get('media_type')
    
    if not tmdb_id or not media_type:
        return jsonify({'error': 'Missing tmdb_id or media_type'}), 400
    
    try:
        # Get raw TMDB metadata first
        media_meta = get_media_meta(str(tmdb_id), media_type)
        if not media_meta:
            return jsonify({'error': 'Could not fetch media metadata from TMDB'}), 400
            
        poster_url, overview, raw_tmdb_genres, vote_average, backdrop_path = media_meta
        
        # Start with raw TMDB genres (like the main scraper does)
        final_genres = raw_tmdb_genres.copy() if raw_tmdb_genres else []
        logging.info(f"get_media_meta_endpoint: Raw TMDB genres: {final_genres}")
        
        # Check for anime detection using get_metadata (same as main scraper)
        try:
            metadata = get_metadata(tmdb_id=int(tmdb_id), item_media_type=media_type)
            if metadata and metadata.get('genres'):
                processed_genres = metadata.get('genres', [])
                logging.info(f"get_media_meta_endpoint: Processed metadata genres: {processed_genres}")
                
                # Check if anime was detected in processed metadata
                if 'anime' in processed_genres and 'anime' not in final_genres:
                    final_genres.append('anime')
                    logging.info(f"get_media_meta_endpoint: Added anime to genres: {final_genres}")
        except Exception as e:
            logging.warning(f"Could not get processed metadata for anime detection: {e}")
        
        logging.info(f"get_media_meta_endpoint: Final combined genres: {final_genres}")
        
        return jsonify({
            'poster_url': poster_url,
            'overview': overview,
            'genres': final_genres,  # Combined raw + anime detection
            'vote_average': vote_average,
            'backdrop_path': backdrop_path
        })
        
    except Exception as e:
        logging.error(f"Error in get_media_meta_endpoint: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    
@scraper_bp.route('/run_scrape', methods=['POST'])
@user_required
@scraper_permission_required
def run_scrape():
    from metadata.metadata import get_metadata, get_release_date, _get_local_timezone, DirectAPI
    data = request.json
    try:
        imdb_id = data.get('imdb_id', '')
        tmdb_id = data.get('tmdb_id', '')
        title = data['title']
        year = data.get('year')
        media_type = data['movie_or_episode']
        version = data['version']
        modified_settings = data.get('modifiedSettings', {})
        genres = data.get('genres', [])
        skip_cache_check = data.get('skip_cache_check', False)  # Default to NOT skipping cache check
        
        if media_type == 'episode':
            season = int(data.get('season', 1))  # Convert to int, default to 1
            episode = int(data.get('episode', 1))  # Convert to int, default to 1
            multi = data.get('multi', False)
        else:
            season = None
            episode = None
            multi = False

        year = int(year) if year else None

        # Load current config and get original version settings
        config = load_config()
        original_version_settings = config['Scraping']['versions'].get(version, {}).copy()
        
        logging.debug(f"[run_scrape_route] Calling scrape for original settings, title '{title}'.")
        # Run first scrape with current settings
        original_results, original_filtered_out_results = scrape(
            imdb_id, tmdb_id, title, year, media_type, version, season, episode, multi, genres, skip_cache_check
        )
        logging.debug(f"[run_scrape_route] Original scrape returned: passed={len(original_results)}, filtered_out={len(original_filtered_out_results if original_filtered_out_results else [])}")

        # Update version settings with modified settings
        updated_version_settings = original_version_settings.copy()
        updated_version_settings.update(modified_settings)

        # Handle special values for max_bitrate_mbps and min_bitrate_mbps
        for key in ['max_bitrate_mbps', 'min_bitrate_mbps']:
            if key in updated_version_settings:
                if updated_version_settings[key] == '' or updated_version_settings[key] is None:
                    updated_version_settings[key] = float('inf') if key.startswith('max_') else 0.0
                else:
                    try:
                        updated_version_settings[key] = float(updated_version_settings[key])
                    except (ValueError, TypeError):
                        updated_version_settings[key] = float('inf') if key.startswith('max_') else 0.0

        # Save modified settings temporarily
        config['Scraping']['versions'][version] = updated_version_settings
        save_config(config)

        # Run second scrape with modified settings
        try:
            logging.debug(f"[run_scrape_route] Calling scrape for adjusted settings, title '{title}'.")
            adjusted_results, adjusted_filtered_out_results = scrape(
                imdb_id, tmdb_id, title, year, media_type, version, season, episode, multi, genres, skip_cache_check
            )
            logging.debug(f"[run_scrape_route] Adjusted scrape returned: passed={len(adjusted_results)}, filtered_out={len(adjusted_filtered_out_results if adjusted_filtered_out_results else [])}")
        finally:
            # Revert settings back to original
            config = load_config()
            config['Scraping']['versions'][version] = original_version_settings
            save_config(config)

        # Ensure score_breakdown is included in the results
        # Also process filtered out results for score_breakdown and cache status
        all_results_to_process = original_results + adjusted_results + original_filtered_out_results + adjusted_filtered_out_results
        for result in all_results_to_process:
            if 'score_breakdown' not in result:
                result['score_breakdown'] = {'total_score': result.get('score', 0)}
            
            # Set default cache status to 'N/A'
            if 'cached' not in result:
                result['cached'] = 'N/A'
        
        # Check cache status for the first 5 results of each main list (not filtered out ones)
        if not skip_cache_check:
            try:
                debrid_provider = get_debrid_provider()
                if getattr(debrid_provider, 'supports_direct_cache_check', False):
                    # Process original results
                    for i, result in enumerate(original_results[:5]):
                        if 'magnet' in result:
                            cache_status = debrid_provider.is_cached(
                                result['magnet'], 
                                result_title=result.get('title', ''),
                                result_index=i
                            )
                            result['cached'] = 'Yes' if cache_status is True else 'No' if cache_status is False else 'Unknown'
                    
                    # Process adjusted results
                    for i, result in enumerate(adjusted_results[:5]):
                        if 'magnet' in result:
                            cache_status = debrid_provider.is_cached(
                                result['magnet'], 
                                result_title=result.get('title', ''),
                                result_index=i
                            )
                            result['cached'] = 'Yes' if cache_status is True else 'No' if cache_status is False else 'Unknown'
            except Exception as e:
                logging.error(f"Error checking cache status: {str(e)}", exc_info=True)
                # Continue without cache status if there's an error
        else:
            logging.info("Skipping cache check as requested")

        logging.debug(f"[run_scrape_route] Returning JSON for '{title}': "
                      f"originalResults={len(original_results)}, originalFilteredOutResults={len(original_filtered_out_results if original_filtered_out_results else [])}, "
                      f"adjustedResults={len(adjusted_results)}, adjustedFilteredOutResults={len(adjusted_filtered_out_results if adjusted_filtered_out_results else [])}")
        return jsonify({
            'originalResults': original_results,
            'adjustedResults': adjusted_results,
            'originalFilteredOutResults': original_filtered_out_results,
            'adjustedFilteredOutResults': adjusted_filtered_out_results
        })
    except Exception as e:
        logging.error(f"Error in run_scrape: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500

@scraper_bp.route('/remove_uncached_item', methods=['POST'])
@user_required
@scraper_permission_required
def remove_uncached_item():
    """Remove an uncached item from the database and debrid provider"""
    try:
        torrent_id = request.form.get('torrent_id')
        torrent_hash = request.form.get('torrent_hash')
        
        if not torrent_id and not torrent_hash:
            return jsonify({'error': 'No torrent ID or hash provided'}), 400
            
        logging.info(f"Removing uncached item with ID: {torrent_id}, hash: {torrent_hash}")
        
        # Remove from debrid provider
        debrid_provider = get_debrid_provider()
        if torrent_id:
            try:
                debrid_provider.remove_torrent(torrent_id, "User removed uncached item")
                logging.info(f"Removed torrent {torrent_id} from debrid provider")
            except Exception as e:
                logging.error(f"Failed to remove torrent from debrid provider: {e}")
        
        # Remove from database if hash is provided
        if torrent_hash:
            from database.torrent_tracking import mark_torrent_removed
            try:
                mark_torrent_removed(torrent_hash, "User removed uncached item")
                logging.info(f"Marked torrent {torrent_hash} as removed in database")
                
                # Also remove from media items table if it exists
                from database import get_db_connection
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("DELETE FROM media_items WHERE filled_by_magnet LIKE ?", (f"%{torrent_hash}%",))
                conn.commit()
                conn.close()
                logging.info(f"Removed media items with hash {torrent_hash} from database")
            except Exception as e:
                logging.error(f"Failed to remove torrent from database: {e}")
        
        return jsonify({
            'success': True,
            'message': 'Successfully removed uncached item'
        })
        
    except Exception as e:
        error_message = str(e)
        logging.error(f"Error in remove_uncached_item: {error_message}")
        return jsonify({'error': error_message}), 500

@scraper_bp.route('/get_tv_details/<tmdb_id>')
def get_tv_details(tmdb_id):
    from metadata.metadata import get_metadata
    from cli_battery.app.direct_api import DirectAPI
    api = DirectAPI()
    try:
        # First get the IMDb ID from TMDB ID
        imdb_id, _ = api.tmdb_to_imdb(str(tmdb_id), media_type='show')
        if not imdb_id:
            return jsonify({'success': False, 'error': 'Could not find IMDb ID for the given TMDB ID'}), 404

        # Get the show metadata
        metadata = get_metadata(imdb_id=imdb_id, tmdb_id=tmdb_id, item_media_type='tv')
        if not metadata:
            return jsonify({'success': False, 'error': 'Could not fetch show metadata'}), 404

        # Extract seasons data
        seasons_data = metadata.get('seasons', {})
        if not seasons_data or seasons_data == 'None':
            return jsonify({'success': False, 'error': 'No seasons data available'}), 404

        # Format the seasons data for the frontend
        formatted_seasons = []
        for season_num, season_data in seasons_data.items():
            if season_num == '0':  # Skip specials
                continue
            
            episodes = season_data.get('episodes', {})
            formatted_seasons.append({
                'season_number': int(season_num),
                'episode_count': len(episodes) if episodes else 0
            })

        # Sort seasons by number
        formatted_seasons.sort(key=lambda x: x['season_number'])

        return jsonify({
            'success': True,
            'seasons': formatted_seasons
        })

    except Exception as e:
        logging.error(f"Error getting TV details: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@scraper_bp.route('/tmdb_image/<path:image_path>')
def tmdb_image_proxy(image_path):
    import requests
    from flask import Response, make_response

    # Sanitize path to prevent directory traversal
    safe_path = image_path.replace('..', '').lstrip('/')

    # Check server-side disk cache first — serves any user/browser without re-fetching from TMDB
    db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    cache_file = os.path.join(db_content_dir, 'image_cache', safe_path)

    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'rb') as f:
                image_data = f.read()
            ext = os.path.splitext(cache_file)[1].lower()
            content_type = 'image/png' if ext == '.png' else 'image/jpeg'
            resp = Response(image_data, content_type=content_type)
            resp.headers['Cache-Control'] = 'public, max-age=604800'
            return resp
        except Exception as e:
            logging.warning(f"Image cache read error for {safe_path}: {e}")
            # Fall through to TMDB fetch

    # Cache miss — fetch from TMDB CDN
    tmdb_url = f'https://image.tmdb.org/t/p/{safe_path}'

    try:
        response = requests.get(tmdb_url, timeout=10)
        response.raise_for_status()

        image_data = response.content
        content_type = response.headers.get('Content-Type', 'image/jpeg')

        # Save to disk cache for future requests
        try:
            os.makedirs(os.path.dirname(cache_file), exist_ok=True)
            with open(cache_file, 'wb') as f:
                f.write(image_data)
        except Exception as e:
            logging.warning(f"Could not write image cache for {safe_path}: {e}")

        proxy_response = Response(image_data, content_type=content_type)
        proxy_response.headers['Cache-Control'] = 'public, max-age=604800'
        return proxy_response

    except requests.RequestException as e:
        logging.error(f"Error proxying TMDB image {safe_path}: {e}")
        return make_response('Image not found', 404)


@scraper_bp.route('/check_cache_status', methods=['POST'])
@user_required
@scraper_permission_required
def check_cache_status():
    try:
        data = request.json
        logging.debug(f"Cache check request data: {data}")
        
        # Use the singleton cache manager
        cache_manager = _phalanx_cache_manager
        
        # Handle single item cache check (new approach)
        if 'index' in data:
            index = data.get('index')
            magnet_link = data.get('magnet_link')
            torrent_url = data.get('torrent_url')
            
            # --- START EDIT: Get imdb_id from request and extract title ---
            imdb_id = data.get('imdb_id')
            if not imdb_id:
                imdb_id = session.get('last_selected_imdb_id')
                if imdb_id:
                    logging.debug(f"Retrieved imdb_id {imdb_id} from session for cache check.")
            
            # Create the item dict for context if imdb_id is available
            item_for_check = {'imdb_id': imdb_id} if imdb_id else None

            logging.debug(f"Processing cache check for item at index {index}")
            
            if not magnet_link and not torrent_url:
                logging.warning(f"No magnet link or torrent URL provided for index {index}")
                return jsonify({'status': 'check_unavailable'}), 200
            
            # Extract hash from magnet link if present - do this up front
            torrent_hash = None
            file_hash = None
            
            if magnet_link:
                # Fast hash extraction
                btih_match = re.search(r'btih:([a-fA-F0-9]{40})', magnet_link, re.IGNORECASE)
                if btih_match:
                    torrent_hash = btih_match.group(1).lower()
                    
                # Handle HTTP URLs - extract file hash for later PhalanxDB update only
                if magnet_link.startswith('http'):
                    file_param = magnet_link.split('&file=')[-1] if '&file=' in magnet_link else None
                    if file_param:
                        file_hash = f"FILE_HASH_{hashlib.sha1(file_param.encode()).hexdigest()}"

            elif torrent_url:
                try:
                    torrent_hash = _download_and_get_hash(torrent_url)
                except Exception as e:
                    logging.warning(f"Could not extract hash from torrent URL: {e}")
            
            # Check all providers: RD uses PhalanxDB, others use direct API
            from debrid import get_debrid_providers
            all_providers = get_debrid_providers()
            cache_providers = {}
            is_cached = None

            for prov in all_providers:
                prov_result = None
                try:
                    if prov.PROVIDER_NAME == 'Real-Debrid':
                        # RD has no direct cache check API — use PhalanxDB
                        if _phalanx_cache_manager and torrent_hash:
                            rd_status = _phalanx_cache_manager.get_cache_status(torrent_hash)
                            prov_result = rd_status.get('is_cached') if rd_status is not None else None
                        prov_status = 'Yes' if prov_result is True else ('No' if prov_result is False else 'N/A')
                    else:
                        prov_processor = TorrentProcessor(prov)
                        if magnet_link:
                            prov_result = prov_processor.check_cache(magnet_link, remove_cached=True, item=None)
                        elif torrent_url:
                            prov_result = prov_processor.check_cache_for_url(torrent_url, remove_cached=True, item=None)
                        prov_status = 'Yes' if prov_result is True else ('No' if prov_result is False else 'N/A')
                except Exception as _pe:
                    logging.warning(f"[{prov.PROVIDER_NAME}] cache check error: {_pe}")
                    prov_status = 'Error'
                cache_providers[prov.PROVIDER_NAME] = prov_status
                if prov_result is True and is_cached is not True:
                    is_cached = True
                elif prov_result is False and is_cached is None:
                    is_cached = False

            # Update PhalanxDB with primary result
            if cache_manager and torrent_hash:
                try:
                    cache_manager.update_cache_status(torrent_hash, bool(is_cached))
                except Exception as e:
                    logging.error(f"Error updating PhalanxDB cache: {str(e)}")
            if cache_manager and file_hash:
                cache_manager.update_cache_status(file_hash, bool(is_cached))

            # Convert result to the expected format
            if is_cached is True:
                status = 'cached'
            elif is_cached is False:
                status = 'not_cached'
            else:
                status = 'check_unavailable'

            logging.debug(f"Returning cache status for index {index}: {status} providers={cache_providers}")
            return jsonify({'status': status, 'index': index, 'cache_providers': cache_providers}), 200
            
        # Handle multiple hashes (legacy approach)
        hashes = data.get('hashes', [])
        if not hashes:
            return jsonify({'error': 'No hashes provided'}), 400
            
        # Always limit to exactly 5 hashes, preserving the order
        if len(hashes) > 5:
            logging.debug(f"Limiting cache check from {len(hashes)} to exactly 5 hashes")
            hashes = hashes[:5]
            
        # First check PhalanxDB for all hashes at once with multi-status
        cache_status = {}
        hashes_to_check = []
        
        # Get results for all hashes at once
        if cache_manager: # Check cache_manager is not None
            phalanx_results = cache_manager.get_multi_cache_status(hashes)
            for hash_value, status in phalanx_results.items():
                if status is not None:
                    cache_status[hash_value] = 'Yes' if status.get('is_cached') else 'No' # Use .get() for safety
                else:
                    hashes_to_check.append(hash_value)
        else:
             hashes_to_check = hashes # If no cache manager, check all hashes with debrid
                
        if hashes_to_check:
            logging.info(f"Need to check {len(hashes_to_check)} hashes with debrid provider")
            # Get the debrid provider and check its capabilities
            debrid_provider = get_debrid_provider()
            supports_cache_check = debrid_provider.supports_direct_cache_check
            supports_bulk_check = debrid_provider.supports_bulk_cache_checking
            # Derive behavior from capabilities instead of concrete type
            is_real_debrid = getattr(debrid_provider, 'supports_direct_cache_check', False)
            
            if supports_cache_check:
                try:
                    # Optimize for single hash requests
                    if len(hashes_to_check) == 1:
                        hash_value = hashes_to_check[0]
                        is_cached = debrid_provider.is_cached(hash_value)
                        cache_status[hash_value] = 'Yes' if is_cached else 'No'
                        # Update PhalanxDB
                        if cache_manager: # Check cache_manager is not None
                            try:
                                cache_manager.update_cache_status(hash_value, bool(is_cached))
                                logging.debug(f"Updated PhalanxDB cache status for hash {hash_value}: {bool(is_cached)}")
                            except Exception as e:
                                logging.error(f"Error updating PhalanxDB cache: {str(e)}")
                    elif supports_bulk_check:
                        bulk_result = debrid_provider.is_cached(hashes_to_check)
                        if isinstance(bulk_result, bool):
                            for hash_value in hashes_to_check:
                                cache_status[hash_value] = 'Yes' if bulk_result else 'No'
                                if cache_manager: # Check cache_manager is not None
                                    try:
                                        cache_manager.update_cache_status(hash_value, bool(bulk_result))
                                        logging.debug(f"Updated PhalanxDB cache status for hash {hash_value}: {bool(bulk_result)}")
                                    except Exception as e:
                                        logging.error(f"Error updating PhalanxDB cache: {str(e)}")
                        else:
                            for hash_value in hashes_to_check:
                                result = bulk_result.get(hash_value, 'N/A')
                                cache_status[hash_value] = 'Yes' if result is True else 'No' if result is False else 'N/A'
                                if result is not None and result != 'N/A': # Check before updating
                                    if cache_manager: # Check cache_manager is not None
                                        try:
                                            cache_manager.update_cache_status(hash_value, bool(result))
                                            logging.debug(f"Updated PhalanxDB cache status for hash {hash_value}: {bool(result)}")
                                        except Exception as e:
                                            logging.error(f"Error updating PhalanxDB cache: {str(e)}")
                    else:
                        for hash_value in hashes_to_check:
                            try:
                                is_cached = debrid_provider.is_cached(hash_value)
                                cache_status[hash_value] = 'Yes' if is_cached else 'No'
                                if cache_manager: # Check cache_manager is not None
                                    try:
                                        cache_manager.update_cache_status(hash_value, bool(is_cached))
                                        logging.debug(f"Updated PhalanxDB cache status for hash {hash_value}: {bool(is_cached)}")
                                    except Exception as e:
                                        logging.error(f"Error updating PhalanxDB cache: {str(e)}")
                            except Exception as e:
                                logging.error(f"Error checking individual cache status for {hash_value}: {e}")
                                cache_status[hash_value] = 'N/A'
                except Exception as e:
                    logging.error(f"Error checking cache status: {e}")
                    for hash_value in hashes_to_check:
                        cache_status[hash_value] = 'N/A'
            elif is_real_debrid:
                logging.info("Using provider's is_cached method based on capability flags")
                torrent_ids_to_remove = []
                
                for i, hash_value in enumerate(hashes_to_check):
                    try:
                        magnet_link = f"magnet:?xt=urn:btih:{hash_value}"
                        cache_result = debrid_provider.is_cached(
                            magnet_link, 
                            result_title=f"Hash {hash_value}",
                            result_index=i,
                            remove_uncached=True
                        )
                        result_str = 'Yes' if cache_result is True else 'No' if cache_result is False else 'N/A'
                        cache_status[hash_value] = result_str
                        
                        if cache_result is not None and cache_result != 'N/A': # Check before updating
                            if cache_manager: # Check cache_manager is not None
                                try:
                                    cache_manager.update_cache_status(hash_value, bool(cache_result))
                                    logging.debug(f"Updated PhalanxDB cache status for hash {hash_value}: {bool(cache_result)}")
                                except Exception as e:
                                    logging.error(f"Error updating PhalanxDB cache: {str(e)}")
                        
                        torrent_id = debrid_provider._all_torrent_ids.get(hash_value)
                        if torrent_id:
                            torrent_ids_to_remove.append(torrent_id)
                    except Exception as e:
                        logging.error(f"Error checking cache for hash {hash_value}: {str(e)}")
                        cache_status[hash_value] = 'N/A'
                
                for torrent_id in torrent_ids_to_remove:
                    try:
                        debrid_provider.remove_torrent(torrent_id, "Removed after cache check")
                    except Exception as e:
                        logging.error(f"Error removing torrent {torrent_id}: {str(e)}")
            else:
                for hash_value in hashes_to_check:
                    cache_status[hash_value] = 'N/A'
                
        # Removed redundant PhalanxDB checks here as they are now handled earlier
        # Ensure PhalanxDB updates only happen if cache_manager exists
        if cache_manager:
            try:
                for hash_value, status_str in cache_status.items():
                    if status_str in ['Yes', 'No']: # Only update if we have a definitive status
                        is_cached = status_str == 'Yes'
                        # Check if this hash already exists in PhalanxDB to avoid redundant updates
                        existing_status = cache_manager.get_cache_status(hash_value)
                        if existing_status is None or existing_status.get('is_cached') != is_cached:
                            cache_manager.update_cache_status(hash_value, is_cached)
                            logging.debug(f"Updated PhalanxDB cache status for hash {hash_value}: {is_cached}")
            except Exception as e:
                logging.error(f"Error updating PhalanxDB cache after debrid check: {str(e)}")

        return jsonify({'cache_status': cache_status})
    except Exception as e:
        logging.error(f"Error in check_cache_status: {str(e)}", exc_info=True)
        return jsonify({'error': 'An error occurred while checking cache status'}), 500

async def _fetch_details_for_id_lookup(id_type: str, media_id: str) -> List[Dict[str, Any]]:
    """
    Helper to fetch media details based on IMDb or TMDb ID.
    Returns a list containing a single result dict if found, else empty list.
    """
    from metadata.metadata import get_metadata
    tmdb_api_key = get_setting('TMDB', 'api_key')
    has_tmdb = bool(tmdb_api_key)
    results = []
    metadata_result = None
    media_type = None

    def is_better_tv_candidate(metadata):
        """
        Determine if metadata suggests this is better classified as a TV show.
        Returns True if TV indicators are strong, False otherwise.
        """
        if not metadata:
            return False
            
        # Strong TV indicators
        airs_data = metadata.get('airs', {})
        if airs_data and isinstance(airs_data, dict):
            # Has day/time/timezone = TV show
            if airs_data.get('day') or airs_data.get('time') or airs_data.get('timezone'):
                logging.info(f"Strong TV indicator found: airs data = {airs_data}")
                return True
        
        # Runtime analysis (episodes typically 20-70 mins, movies typically 80+ mins)
        runtime = metadata.get('runtime')
        if runtime and isinstance(runtime, int):
            if runtime <= 70:  # Likely episode runtime
                logging.info(f"TV runtime indicator: {runtime} minutes (typical episode length)")
                return True
            elif runtime >= 120:  # Likely movie runtime  
                logging.info(f"Movie runtime indicator: {runtime} minutes (typical movie length)")
                return False
        
        # Check for seasons data
        seasons = metadata.get('seasons', {})
        if seasons and isinstance(seasons, dict) and len(seasons) > 0:
            logging.info(f"Strong TV indicator: seasons data found with {len(seasons)} seasons")
            return True
            
        return False

    def calculate_metadata_quality_score(metadata):
        """Calculate a quality score for metadata completeness."""
        if not metadata:
            return 0
            
        score = 0
        # Basic fields
        if metadata.get('title'): score += 10
        if metadata.get('year'): score += 10
        if metadata.get('genres'): score += 5
        if metadata.get('tmdb_id'): score += 15
        
        # Rich data indicators
        if metadata.get('overview'): score += 5
        if metadata.get('runtime'): score += 5
        if metadata.get('airs'): score += 20  # TV shows have this
        if metadata.get('seasons'): score += 25  # TV shows have this
        if metadata.get('release_date') or metadata.get('first_aired'): score += 10
        
        return score

    try:
        movie_metadata = None
        tv_metadata = None
        
        # Try both movie and TV lookups for IMDb IDs
        if id_type == 'imdb':
            logging.info(f"Trying both movie and TV lookups for IMDb ID: {media_id}")
            
            # Try movie lookup
            try:
                movie_metadata = get_metadata(imdb_id=media_id, item_media_type='movie')
                if movie_metadata:
                    logging.debug(f"Movie metadata found for {media_id}")
            except Exception as e:
                logging.debug(f"Movie lookup failed for {media_id}: {e}")
            
            # Try TV lookup  
            try:
                tv_metadata = get_metadata(imdb_id=media_id, item_media_type='tv')
                if tv_metadata:
                    logging.debug(f"TV metadata found for {media_id}")
            except Exception as e:
                logging.debug(f"TV lookup failed for {media_id}: {e}")
                
        elif id_type == 'tmdb':
            try:
                media_id_int = int(media_id)
                logging.info(f"Trying both movie and TV lookups for TMDB ID: {media_id_int}")
                
                # Try movie lookup
                try:
                    movie_metadata = get_metadata(tmdb_id=media_id_int, item_media_type='movie')
                    if movie_metadata:
                        logging.debug(f"Movie metadata found for TMDB {media_id_int}")
                except Exception as e:
                    logging.debug(f"Movie lookup failed for TMDB {media_id_int}: {e}")
                
                # Try TV lookup
                try:
                    tv_metadata = get_metadata(tmdb_id=media_id_int, item_media_type='tv')
                    if tv_metadata:
                        logging.debug(f"TV metadata found for TMDB {media_id_int}")
                except Exception as e:
                    logging.debug(f"TV lookup failed for TMDB {media_id_int}: {e}")
                    
            except ValueError:
                logging.error(f"Invalid TMDb ID format (after stripping prefix): {media_id}")
                return []

        # Now decide which result to use based on analysis
        if movie_metadata and tv_metadata:
            logging.info(f"Both movie and TV metadata found for {id_type}={media_id}, analyzing to determine best match...")
            
            # Check if TV metadata has strong TV indicators
            if is_better_tv_candidate(tv_metadata):
                logging.info(f"TV metadata has strong indicators, using TV result for {media_id}")
                metadata_result = tv_metadata
                media_type = 'tv'
            elif is_better_tv_candidate(movie_metadata):
                # This shouldn't happen often, but handle edge case
                logging.warning(f"Movie metadata has TV indicators, switching to TV result for {media_id}")
                metadata_result = tv_metadata if tv_metadata else movie_metadata
                media_type = 'tv'
            else:
                # Compare quality scores
                movie_score = calculate_metadata_quality_score(movie_metadata)
                tv_score = calculate_metadata_quality_score(tv_metadata) 
                
                logging.info(f"Quality scores for {media_id}: movie={movie_score}, tv={tv_score}")
                
                if tv_score > movie_score:
                    logging.info(f"TV metadata has higher quality score, using TV result for {media_id}")
                    metadata_result = tv_metadata
                    media_type = 'tv'
                else:
                    logging.info(f"Movie metadata has higher/equal quality score, using movie result for {media_id}")
                    metadata_result = movie_metadata
                    media_type = 'movie'
                    
        elif tv_metadata:
            logging.info(f"Only TV metadata found for {media_id}")
            metadata_result = tv_metadata
            media_type = 'tv'
        elif movie_metadata:
            logging.info(f"Only movie metadata found for {media_id}")
            metadata_result = movie_metadata
            media_type = 'movie'
        else:
            logging.warning(f"No metadata found for {id_type} ID: {media_id}")
            return []

        if metadata_result and media_type:
            tmdb_id = metadata_result.get('tmdb_id')
            title = metadata_result.get('title', 'N/A')
            year = metadata_result.get('year', 'N/A')
            overview = metadata_result.get('overview', '')
            release_date = metadata_result.get('release_date') if media_type == 'movie' else metadata_result.get('first_aired')

            poster_path_final = None # Use a distinct variable name
            genres = []
            vote_average = 0.0
            backdrop_path_full = None

            media_meta_tuple = await asyncio.to_thread(
                get_media_meta, str(tmdb_id), media_type
            )

            if media_meta_tuple:
                 # Use distinct variable for poster path from tuple
                 poster_path_from_meta, _, genres, vote_average, backdrop_path_rel = media_meta_tuple

                 # Assign to final poster path, preferring the one from get_media_meta
                 poster_path_final = poster_path_from_meta

                 if not has_tmdb and (not poster_path_final or 'placeholder' not in poster_path_final):
                     poster_path_final = "static/images/placeholder.png"
                 elif has_tmdb and not poster_path_final:
                      logging.warning(f"Could not retrieve poster for {media_type} {title} (TMDb: {tmdb_id})")
                      poster_path_final = "static/images/placeholder.png"

                 if backdrop_path_rel:
                     backdrop_path_full = f"https://image.tmdb.org/t/p/original{backdrop_path_rel}"
            else:
                 if not has_tmdb:
                      poster_path_final = "static/images/placeholder.png"
                 else:
                      logging.warning(f"get_media_meta failed for {media_type} {title} (TMDb: {tmdb_id}). Using placeholder.")
                      poster_path_final = "static/images/placeholder.png"

            formatted_result = {
                'media_type': 'show' if media_type == 'tv' else media_type, # Normalize tv to show for JS
                'id': str(tmdb_id),
                'title': title,
                'year': year,
                'poster_path': poster_path_final, # Use final path with snake_case
                'overview': overview, # Use corrected overview
                'genres': genres,
                'voteAverage': vote_average,
                'backdrop_path': backdrop_path_full, # Use full URL
                'release_date': release_date, # Use corrected release_date
                'imdb_id': metadata_result.get('imdb_id')
            }
            results.append(formatted_result)
            
            # Log the final decision for debugging
            logging.info(f"Final classification for {id_type}={media_id}: {media_type} - '{title}' ({year})")
        else:
            logging.warning(f"Could not find metadata for {id_type} ID: {media_id}")

    except Exception as e:
        logging.error(f"Error during ID lookup's async helper ({id_type}={media_id}): {e}", exc_info=True)
    return results

@scraper_bp.route('/lookup_by_id', methods=['POST'])
@user_required
@scraper_view_access_required
@onboarding_required
def lookup_by_id(): # This remains synchronous
    id_type = request.form.get('id_type')
    media_id = request.form.get('media_id')

    if not id_type or not media_id or id_type not in ['imdb', 'tmdb']:
        return jsonify({'error': 'Invalid ID type or ID provided'}), 400

    logging.info(f"Performing ID lookup: type={id_type}, id={media_id}")

    results = asyncio.run(_fetch_details_for_id_lookup(id_type, media_id))

    if not results:
        return jsonify({'error': 'Media not found for the provided ID'}), 404

    # --- Simplify result processing - main processing done in helper ---
    processed_results = []
    for item in results: # item already has snake_case keys
        tmdb_id_val = item.get('id')
        db_state = 'Missing'
        if tmdb_id_val:
            try:
                tmdb_id_int = int(tmdb_id_val)
                db_state = get_media_item_presence_overall(tmdb_id=tmdb_id_int)
            except (ValueError, TypeError):
                logging.warning(f"Could not parse tmdb_id_val for db_state check: {tmdb_id_val}")
                db_state = 'Missing'

        item['db_status'] = {
            'Collected': 'collected',
            'Partial': 'partial',
            'Blacklisted': 'blacklisted'
        }.get(db_state, 'processing' if db_state not in ['Missing', 'Ignored', None] else 'missing')

        # Ensure necessary fields are present (redundant check, but safe)
        if 'poster_path' not in item or not item['poster_path']:
             item['poster_path'] = "static/images/placeholder.png"
        if 'year' not in item and item.get('release_date'):
             item['year'] = str(item['release_date'])[:4]
        # Remove inconsistent keys if they somehow slipped through (unlikely now)
        item.pop('mediaType', None)
        item.pop('posterPath', None)
        item.pop('show_overview', None)
        item.pop('backdropPath', None)

        processed_results.append(item)
    # --- End Simplified Processing ---

    logging.info(f"ID lookup successful, returning {len(processed_results)} result(s).")
    return jsonify({'results': processed_results})

@scraper_bp.route('/get_symlink_folders')
@user_required
def get_symlink_folders():
    """
    Get available symlink folders for the folder dropdown in torrent modal.
    Returns folder information including:
    - All enabled standard folders from settings (Movies, TV Shows, Anime, Documentary)
    - Custom folders that physically exist in the symlink path
    - Which folders are custom (need /Movies or /TV Shows subfolders)
    - Whether each folder exists on the filesystem yet
    - Folder name settings for auto-selection logic
    """
    try:
        # Check if symlinking is enabled
        file_management_mode = get_setting('File Management', 'file_collection_management', 'Plex')
        if file_management_mode != 'Symlinked/Local':
            return jsonify({'folders': [], 'enabled': False})

        symlink_path = get_setting('File Management', 'symlinked_files_path', '/mnt/symlinked')

        # Get folder name settings
        folder_settings = {
            'movies_folder_name': get_setting('Debug', 'movies_folder_name', 'Movies'),
            'tv_shows_folder_name': get_setting('Debug', 'tv_shows_folder_name', 'TV Shows'),
            'anime_movies_folder_name': get_setting('Debug', 'anime_movies_folder_name', 'Anime Movies'),
            'anime_tv_shows_folder_name': get_setting('Debug', 'anime_tv_shows_folder_name', 'Anime TV Shows'),
            'documentary_movies_folder_name': get_setting('Debug', 'documentary_movies_folder_name', 'Documentary Movies'),
            'documentary_tv_shows_folder_name': get_setting('Debug', 'documentary_tv_shows_folder_name', 'Documentary TV Shows'),
            'enable_separate_anime_folders': get_setting('Debug', 'enable_separate_anime_folders', False),
            'enable_separate_documentary_folders': get_setting('Debug', 'enable_separate_documentary_folders', False)
        }

        # Build list of standard folders
        standard_folders = [
            folder_settings['movies_folder_name'],
            folder_settings['tv_shows_folder_name']
        ]

        if folder_settings['enable_separate_anime_folders']:
            standard_folders.extend([
                folder_settings['anime_movies_folder_name'],
                folder_settings['anime_tv_shows_folder_name']
            ])

        if folder_settings['enable_separate_documentary_folders']:
            standard_folders.extend([
                folder_settings['documentary_movies_folder_name'],
                folder_settings['documentary_tv_shows_folder_name']
            ])

        # Get custom folders from content sources
        custom_folders = []
        all_settings = get_all_settings()
        content_sources = all_settings.get('Content Sources', {})
        for source_id, source_config in content_sources.items():
            if isinstance(source_config, dict):
                custom_folder = source_config.get('custom_symlink_subfolder', '').strip()
                if custom_folder and custom_folder not in custom_folders:
                    custom_folders.append(custom_folder)

        # Scan filesystem to see which folders actually exist
        existing_folder_names = []
        if os.path.exists(symlink_path):
            for folder_name in os.listdir(symlink_path):
                folder_full_path = os.path.join(symlink_path, folder_name)
                if os.path.isdir(folder_full_path):
                    existing_folder_names.append(folder_name)

        # Build comprehensive folder list:
        # 1. All enabled standard folders (from settings, regardless of existence)
        # 2. All custom folders (from content source settings, regardless of existence)
        all_folders = []

        # Add standard folders (always include if enabled in settings)
        for folder_name in standard_folders:
            folder_name_lower = folder_name.lower()
            all_folders.append({
                'name': folder_name,
                'is_custom': False,
                'exists': folder_name in existing_folder_names,
                # Add media type permissions based on folder name (for mobile view filtering)
                'allowed_for_movies': 'movie' in folder_name_lower,
                'allowed_for_tv_shows': 'show' in folder_name_lower or 'tv' in folder_name_lower
            })

        # Add ALL custom folders from content source settings (regardless of existence)
        for folder_name in custom_folders:
            all_folders.append({
                'name': folder_name,
                'is_custom': True,
                'exists': folder_name in existing_folder_names,
                # Custom folders work for both movies and TV shows (they have subfolders)
                'allowed_for_movies': True,
                'allowed_for_tv_shows': True
            })

        return jsonify({
            'folders': all_folders,
            'folder_settings': folder_settings,
            'enabled': True
        })

    except Exception as e:
        logging.error(f"Error getting symlink folders: {str(e)}", exc_info=True)
        return jsonify({'error': str(e), 'enabled': False}), 500


@scraper_bp.route('/get_nzb_files', methods=['POST'])
@user_required
@scraper_permission_required
def get_nzb_files():
    """Fetch an NZB URL and return its file list parsed from the XML."""
    try:
        import xml.etree.ElementTree as ET
        from routes.api_tracker import api as _nzb_api

        data = request.json or {}
        nzb_url = data.get('nzb_url', '')
        if not nzb_url:
            return jsonify({'success': False, 'error': 'No NZB URL provided'}), 400

        r = _nzb_api.get(nzb_url, timeout=15, allow_redirects=True)
        if r.status_code != 200 or '<nzb' not in r.text.lower():
            return jsonify({'success': False, 'error': f'Failed to fetch NZB (status {r.status_code})'}), 400

        root = ET.fromstring(r.text)
        ns = {'nzb': 'http://www.newzbin.com/DTD/2003/nzb'}

        # Try with and without namespace
        files_el = root.findall('.//file') or root.findall('.//nzb:file', ns)

        files = []
        for f in files_el:
            subject = f.get('subject', '')
            # Extract filename from subject (yEnc format: "filename (part/total)")
            import re as _re
            m = _re.search(r'"([^"]+)"', subject)
            name = m.group(1) if m else subject[:80]
            # Skip par2 files
            if name.lower().endswith('.par2') or '.par2.' in name.lower():
                continue
            # Sum segment sizes
            size_bytes = sum(int(seg.get('bytes', 0)) for seg in f.findall('.//segment') or f.findall('.//nzb:segment', ns))
            size_gb = size_bytes / (1024 ** 3)
            if size_gb >= 0.1:
                size_fmt = f'{size_gb:.2f} GB'
            else:
                size_fmt = f'{size_bytes / (1024 ** 2):.1f} MB'
            files.append({'name': name, 'path': name, 'size': size_bytes, 'size_formatted': size_fmt})

        files.sort(key=lambda x: x['name'])
        return jsonify({'success': True, 'files': files, 'total_files': len(files),
                        'metadata': {'filename': data.get('title', ''), 'hash': '', 'status': 'nzb'}})
    except Exception as e:
        logging.error(f'get_nzb_files error: {e}', exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@scraper_bp.route('/get_torrent_files', methods=['POST'])
@user_required
@scraper_permission_required
def get_torrent_files():
    """
    Get file list from a torrent magnet link.
    Extracts hash from magnet and queries debrid provider for file information.

    Request JSON:
        - magnet: Magnet link
        - torrent_title: Title of torrent (for logging)

    Returns:
        - success: Boolean
        - files: List of file objects with name, size, size_formatted, path
        - total_files: Total number of files
        - torrent_hash: Hash extracted from magnet
        - method: How files were retrieved ('instant_availability' or 'cached' or 'error')
        - error: Error message if failed
    """
    try:
        data = request.json
        magnet = data.get('magnet', '')
        torrent_title = data.get('torrent_title', 'Unknown')

        if not magnet:
            return jsonify({'success': False, 'error': 'No magnet link provided'}), 400

        # Extract hash from magnet link
        from debrid.common import extract_hash_from_magnet
        torrent_hash = extract_hash_from_magnet(magnet)

        if not torrent_hash:
            logging.warning(f"Could not extract hash from magnet for '{torrent_title}'")
            return jsonify({'success': False, 'error': 'Invalid magnet link - could not extract hash'}), 400

        logging.info(f"Getting file list for torrent: '{torrent_title}' (hash: {torrent_hash[:16]}...)")

        # Check cache first (15 minute TTL)
        cache_key = f"torrent_files_{torrent_hash}"
        cached_data = get_from_cache(cache_key)
        if cached_data:
            logging.info(f"Returning cached file list for {torrent_hash[:16]}...")
            cached_data['cached'] = True
            return jsonify(cached_data)

        # Get debrid provider
        debrid_provider = get_debrid_provider()

        files_list = []
        method = 'unknown'
        torrent_info = None  # Store torrent info for metadata

        # Method 1: Try instant availability (preferred - doesn't add torrent)
        try:
            if hasattr(debrid_provider, 'is_cached'):
                logging.debug(f"Checking instant availability for {torrent_hash[:16]}...")

                # For Real-Debrid, is_cached can return file info
                cache_result = debrid_provider.is_cached(magnet)

                # If cached, try to get file info from provider
                if cache_result:
                    logging.debug(f"Torrent is cached, attempting to get file list...")

                    # Try to get torrent info if it exists in user's account
                    if hasattr(debrid_provider, 'get_all_torrents'):
                        try:
                            all_torrents = debrid_provider.get_all_torrents()
                            matching_torrent = None

                            for torrent in all_torrents:
                                if torrent.get('hash', '').lower() == torrent_hash.lower():
                                    matching_torrent = torrent
                                    break

                            if matching_torrent:
                                torrent_id = matching_torrent.get('id')
                                if torrent_id and hasattr(debrid_provider, 'get_torrent_info'):
                                    torrent_info_temp = debrid_provider.get_torrent_info(torrent_id)
                                    if torrent_info_temp and 'files' in torrent_info_temp:
                                        files_list = torrent_info_temp.get('files', [])
                                        torrent_info = torrent_info_temp  # Store for metadata
                                        method = 'existing_torrent'
                                        logging.info(f"Retrieved {len(files_list)} files from existing torrent")
                        except Exception as e:
                            logging.debug(f"Could not retrieve from existing torrents: {e}")
        except Exception as e:
            logging.debug(f"Instant availability check failed: {e}")

        # Method 2: Add torrent temporarily to get file list (fallback)
        if not files_list:
            logging.info(f"Adding torrent temporarily to retrieve file list...")
            try:
                # Resolve HTTP URLs to actual magnet links (for Jackett, etc.)
                from debrid.common.torrent import resolve_to_magnet
                actual_magnet = magnet
                if magnet.startswith('http'):
                    logging.info(f"Resolving HTTP URL to magnet link...")
                    resolved = resolve_to_magnet(magnet)
                    if resolved:
                        actual_magnet = resolved
                        logging.info(f"Resolved to magnet link: {resolved[:60]}...")
                    else:
                        raise Exception("Failed to resolve HTTP URL to magnet link")

                # Use get_torrent_file_list if available — it handles polling for slow providers
                if hasattr(debrid_provider, 'get_torrent_file_list'):
                    result = debrid_provider.get_torrent_file_list(actual_magnet)
                    if result:
                        files_list, _fname, _tid = result
                        method = 'temporary_add'
                        logging.info(f"Retrieved {len(files_list)} files via get_torrent_file_list")
                else:
                    # Fallback: raw add + poll
                    torrent_id = None
                    try:
                        torrent_id = debrid_provider.add_torrent(actual_magnet)
                        if not torrent_id:
                            raise Exception("Failed to add torrent - no ID returned")
                        time.sleep(3)
                        if hasattr(debrid_provider, 'get_torrent_info'):
                            torrent_info_temp = debrid_provider.get_torrent_info(torrent_id)
                            if torrent_info_temp:
                                files_list = torrent_info_temp.get('files', [])
                                torrent_info = torrent_info_temp
                                method = 'temporary_add'
                                logging.info(f"Retrieved {len(files_list)} files from temporarily added torrent")
                    finally:
                        if torrent_id and hasattr(debrid_provider, 'remove_torrent'):
                            try:
                                debrid_provider.remove_torrent(torrent_id, "Temporary file list retrieval")
                            except Exception:
                                pass

            except Exception as e:
                logging.error(f"Error adding torrent temporarily: {e}")
                return jsonify({
                    'success': False,
                    'error': f'Could not retrieve file list: {str(e)}'
                }), 500

        # Process and format file list
        if not files_list:
            return jsonify({
                'success': False,
                'error': 'No files found in torrent'
            }), 404

        # Ensure files_list is a list
        if isinstance(files_list, dict):
            files_list = list(files_list.values())

        # Format file sizes and prepare response
        def format_file_size(bytes_size):
            """Format bytes to human-readable size"""
            try:
                bytes_size = float(bytes_size)
            except (ValueError, TypeError):
                return "0 B"

            for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
                if bytes_size < 1024.0:
                    return f"{bytes_size:.2f} {unit}"
                bytes_size /= 1024.0
            return f"{bytes_size:.2f} PB"

        formatted_files = []
        for file_info in files_list:
            # Handle different provider formats
            file_name = file_info.get('path') or file_info.get('name') or 'Unknown'
            file_size = file_info.get('bytes') or file_info.get('size') or 0

            formatted_files.append({
                'name': os.path.basename(file_name),
                'path': file_name,
                'size': file_size,
                'size_formatted': format_file_size(file_size)
            })

        # Sort by size (largest first)
        formatted_files.sort(key=lambda x: x['size'], reverse=True)

        # Extract metadata from torrent_info if available
        torrent_metadata = {
            'id': torrent_info.get('id') if torrent_info else None,
            'hash': torrent_info.get('hash') if torrent_info else torrent_hash,
            'filename': torrent_info.get('filename') if torrent_info else torrent_title,
            'status': torrent_info.get('status') if torrent_info else 'unknown'
        }

        response_data = {
            'success': True,
            'files': formatted_files,
            'total_files': len(formatted_files),
            'torrent_hash': torrent_hash,
            'method': method,
            'metadata': torrent_metadata
        }

        # Cache the result for 15 minutes
        set_in_cache(cache_key, response_data, ttl_seconds=900)

        logging.info(f"Successfully retrieved {len(formatted_files)} files for '{torrent_title}'")
        return jsonify(response_data)

    except Exception as e:
        logging.error(f"Error getting torrent files: {str(e)}", exc_info=True)
        return jsonify({
            'success': False,
            'error': f'Internal error: {str(e)}'
        }), 500
