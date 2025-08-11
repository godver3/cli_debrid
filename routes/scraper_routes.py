from flask import jsonify, request, render_template, session, Blueprint
import logging
from debrid import get_debrid_provider
# Provider-agnostic: avoid direct Real-Debrid import
from .models import user_required, onboarding_required, admin_required, scraper_permission_required, scraper_view_access_required
from utilities.settings import get_setting, get_all_settings, load_config, save_config
from database.database_reading import get_all_season_episode_counts, get_media_item_presence_overall
from utilities.web_scraper import trending_movies, trending_shows, trending_anime, web_scrape, web_scrape_tvshow, process_media_selection, process_torrent_selection
from utilities.web_scraper import get_media_details
from scraper.scraper import scrape
from utilities.manual_scrape import get_details
from utilities.web_scraper import search_trakt
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
import asyncio
from utilities.phalanx_db_cache_manager import PhalanxDBClassManager
import re
import time
import json
from utilities.web_scraper import get_media_meta
from typing import List, Dict, Any, Optional
import iso8601
from utilities.reverse_parser import parse_filename_for_version # Added import

scraper_bp = Blueprint('scraper', __name__)

# Initialize cache manager only if enabled
_phalanx_cache_manager = PhalanxDBClassManager() if get_setting('UI Settings', 'enable_phalanx_db', default=False) else None

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
        # --- START EDIT: Get current_score from form data ---
        current_score_str = request.form.get('current_score', '0') # Default to '0'
        try:
            current_score = float(current_score_str)
        except (ValueError, TypeError):
            logging.warning(f"Invalid current_score value received: '{current_score_str}'. Defaulting to 0.")
            current_score = 0.0
        # --- END EDIT ---

        logging.info(f"Adding {title} ({year}) to debrid provider")

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

        # Add magnet/torrent to debrid provider
        debrid_provider = get_debrid_provider()
        try:
            # Use 'actual_magnet_to_add' which could be the original magnet, 
            # the redirected magnet, or None if a temp_file was successfully created.
            # The debrid_provider.add_torrent method should prioritize temp_file if provided.
            torrent_id = debrid_provider.add_torrent(actual_magnet_to_add, temp_file)
            logging.info(f"Torrent result: {torrent_id}")
            
            if not torrent_id:
                error_message = "Failed to add torrent to debrid provider"
                logging.error(error_message)
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
                    'genres': genres
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

        # Process the content
        processor = ContentProcessor()
        success, message = processor.process_content(torrent_info)
        
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

                # Create media item
                item = {
                    'title': title,
                    'year': year,
                    'type': 'episode' if media_type in ['tv', 'show'] else 'movie',
                    'version': final_version_for_item, # Use the determined version
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
                    'real_debrid_original_title': torrent_info.get('original_filename')
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
                                # --- START EDIT: Add content_source ---
                                episode_item['content_source'] = 'content_requester'
                                # --- END EDIT ---
                                
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
                                    episode_item['filled_by_file'] = matching_filepath_basename # Use the basename
                                    
                                    # Add episode to database
                                    from database import add_media_item
                                    episode_id = add_media_item(episode_item)
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

                    return jsonify({
                        'success': True,
                        'message': 'Successfully processed season pack',
                        'cache_status': cache_status
                    })
                else:
                    # For single episodes or movies, proceed as normal
                    from database import add_media_item

                    item_id = add_media_item(item)
                    if not item_id:
                        raise Exception("Failed to add item to database")
                    
                    # Add the database ID to the item
                    item['id'] = item_id
                    
                    # Add item to checking queue
                    from queues.checking_queue import CheckingQueue
                    checking_queue = CheckingQueue()
                    checking_queue.add_item(item)
                    logging.info(f"Added item to checking queue: {item}")
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

@scraper_bp.route('/', methods=['GET', 'POST'])
@user_required
@scraper_view_access_required
@onboarding_required
def index():
    from utilities.web_scraper import get_available_versions, web_scrape

    versions = get_available_versions()
    # Check if the user is a requester
    is_requester = current_user.is_authenticated and current_user.role == 'requester'
    
    if request.method == 'POST':
        search_term = request.form.get('search_term')
        version = request.form.get('version')
        if search_term:
            session['search_term'] = search_term  # Store the search term in the session
            session['version'] = version  # Store the version in the session
            
            # Allow requesters to search and see results
            results = web_scrape(search_term, version)
            
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
    
    # Pass the is_requester flag to the template
    return render_template('scraper.html', versions=versions, tmdb_api_key_set=tmdb_api_key_set, is_requester=is_requester)

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

        # Return the results
        logging.debug(f"[select_media_route] Returning JSON for '{title}': passed_results={len(passed_results)}, filtered_out_results_list={len(filtered_out_results_list if filtered_out_results_list else [])}")
        return jsonify({
            'torrent_results': passed_results,
            'filtered_out_torrent_results': filtered_out_results_list 
        })
    except Exception as e:
        logging.error(f"Error in select_media: {str(e)}", exc_info=True)
        return jsonify({'error': 'An error occurred while processing your request'}), 500

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
    from flask import Response, make_response, request
    from datetime import datetime, timedelta

    # Log if this is a fresh request or from browser cache
    if_none_match = request.headers.get('If-None-Match')
    if_modified_since = request.headers.get('If-Modified-Since')
    
    if if_none_match or if_modified_since:
        logging.info(f"Browser requesting image with cache validation: {image_path}")
    else:
        logging.info(f"Fresh image request from browser: {image_path}")

    # Construct TMDB URL
    tmdb_url = f'https://image.tmdb.org/t/p/{image_path}'
    
    try:
        # Get the image from TMDB
        response = requests.get(tmdb_url, stream=True)
        response.raise_for_status()
        
        # Create Flask response with the image content
        proxy_response = Response(
            response.iter_content(chunk_size=8192),
            content_type=response.headers['Content-Type']
        )
        
        # Set cache control headers - cache for 7 days
        proxy_response.headers['Cache-Control'] = 'public, max-age=604800'  # 7 days in seconds
        proxy_response.headers['Expires'] = (datetime.utcnow() + timedelta(days=7)).strftime('%a, %d %b %Y %H:%M:%S GMT')
        
        # Add ETag for cache validation
        proxy_response.headers['ETag'] = response.headers.get('ETag', '')
        
        # Log successful image fetch
        logging.info(f"Successfully fetched and cached image from TMDB: {image_path}")
        
        return proxy_response
        
    except requests.RequestException as e:
        logging.error(f"Error proxying TMDB image: {e}")
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
                    
                    # Check PhalanxDB immediately using the hash
                    if _phalanx_cache_manager and torrent_hash:
                        try:
                            cache_status = _phalanx_cache_manager.get_cache_status(torrent_hash)
                            logging.debug(f"Found cache status in PhalanxDB for hash {torrent_hash}: {cache_status}")
                            # If found in PhalanxDB, return early (assuming PhalanxDB is reliable)
                            if cache_status is not None:
                                return jsonify({'status': 'cached' if cache_status.get('is_cached') else 'not_cached', 'index': index}), 200
                        except Exception as e:
                            logging.error(f"Error checking PhalanxDB cache: {str(e)}")
                
                # Handle HTTP URLs - only extract file hash if we need to
                if magnet_link.startswith('http'):
                    file_param = magnet_link.split('&file=')[-1] if '&file=' in magnet_link else None
                    if file_param:
                        # Generate hash of the file name
                        file_hash = f"FILE_HASH_{hashlib.sha1(file_param.encode()).hexdigest()}"
                        
                        # Check if this file hash is already cached
                        # Add check for cache_manager being None
                        if cache_manager: 
                            cache_status = cache_manager.get_cache_status(file_hash)
                            if cache_status is not None:
                                logging.debug(f"File hash {file_hash} found in cache with status: {cache_status}")
                                return jsonify({'status': 'cached' if cache_status.get('is_cached') else 'not_cached', 'index': index}), 200
            
            elif torrent_url:
                # Try to get hash from torrent URL
                try:
                    torrent_hash = _download_and_get_hash(torrent_url)
                    
                    # Check PhalanxDB immediately using the hash
                    if _phalanx_cache_manager and torrent_hash:
                        try:
                            cache_status = _phalanx_cache_manager.get_cache_status(torrent_hash)
                            logging.debug(f"Found cache status in PhalanxDB for torrent hash {torrent_hash}: {cache_status}")
                            # If found in PhalanxDB, return early
                            if cache_status is not None:
                                return jsonify({'status': 'cached' if cache_status.get('is_cached') else 'not_cached', 'index': index}), 200
                        except Exception as e:
                            logging.error(f"Error checking PhalanxDB cache: {str(e)}")
                except Exception as e:
                    logging.warning(f"Could not extract hash from torrent URL: {e}")
            
            # If we reach here, we need to check with the debrid provider
            # Get the debrid provider
            debrid_provider = get_debrid_provider()
            
            # Create a torrent processor with the debrid provider
            torrent_processor = TorrentProcessor(debrid_provider)
            
            # Check cache status based on what we have
            is_cached = None
            if magnet_link:
                logging.info(f"Checking cache status for magnet link at index {index}")
                is_cached = torrent_processor.check_cache(magnet_link, remove_cached=True, item=item_for_check)
                
                # Update PhalanxDB with new cache status if enabled
                if cache_manager and torrent_hash: # Check cache_manager is not None
                    try:
                        cache_manager.update_cache_status(torrent_hash, bool(is_cached))
                        logging.debug(f"Updated PhalanxDB cache status for hash {torrent_hash}: {bool(is_cached)}")
                    except Exception as e:
                        logging.error(f"Error updating PhalanxDB cache: {str(e)}")
                
                # Update cache status for file hash if it was a URL
                if cache_manager and file_hash: # Check cache_manager is not None
                    cache_manager.update_cache_status(file_hash, bool(is_cached))
                    logging.debug(f"Updated cache status for file hash {file_hash}: {bool(is_cached)}")
                    
            elif torrent_url:
                logging.info(f"Checking cache status for torrent URL at index {index}")
                is_cached = torrent_processor.check_cache_for_url(torrent_url, remove_cached=True, item=item_for_check)
                
                # Update PhalanxDB with new cache status if enabled
                if cache_manager and torrent_hash: # Check cache_manager is not None
                    try:
                        cache_manager.update_cache_status(torrent_hash, bool(is_cached))
                        logging.debug(f"Updated PhalanxDB cache status for torrent hash {torrent_hash}: {bool(is_cached)}")
                    except Exception as e:
                        logging.error(f"Error updating PhalanxDB cache: {str(e)}")
            
            # Convert result to the expected format
            if is_cached is True:
                status = 'cached'
            elif is_cached is False:
                status = 'not_cached'
            else:
                status = 'check_unavailable'
                
            logging.debug(f"Returning cache status for index {index}: {status}")
            return jsonify({'status': status, 'index': index}), 200
            
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
