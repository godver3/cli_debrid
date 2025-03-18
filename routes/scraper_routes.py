from flask import jsonify, request, render_template, session, Blueprint
import logging
from debrid import get_debrid_provider
from debrid.real_debrid.client import RealDebridProvider
from .models import user_required, onboarding_required, admin_required, scraper_permission_required, scraper_view_access_required
from settings import get_setting, get_all_settings, load_config, save_config
from database.database_reading import get_all_season_episode_counts
from web_scraper import trending_movies, trending_shows, web_scrape, web_scrape_tvshow, process_media_selection, process_torrent_selection
from web_scraper import get_media_details
from scraper.scraper import scrape
from utilities.manual_scrape import get_details
from web_scraper import search_trakt
from database.database_reading import get_all_season_episode_counts
from metadata.metadata import get_imdb_id_if_missing, get_metadata, get_release_date, _get_local_timezone, DirectAPI
from queues.torrent_processor import TorrentProcessor
from queues.media_matcher import MediaMatcher
from guessit import guessit
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
import re

scraper_bp = Blueprint('scraper', __name__)

@scraper_bp.route('/convert_tmdb_to_imdb/<int:tmdb_id>')
def convert_tmdb_to_imdb(tmdb_id):
    imdb_id = get_imdb_id_if_missing({'tmdb_id': tmdb_id})
    return jsonify({'imdb_id': imdb_id or 'N/A'})

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
    try:
        magnet_link = request.form.get('magnet_link')
        title = request.form.get('title')
        year = request.form.get('year')
        media_type = request.form.get('media_type')
        season = request.form.get('season')
        episode = request.form.get('episode')
        version = request.form.get('version')
        tmdb_id = request.form.get('tmdb_id')

        logging.info(f"Adding {title} ({year}) to debrid provider")

        # Get metadata to determine genres
        metadata = get_metadata(tmdb_id=tmdb_id, item_media_type=media_type) if tmdb_id else {}
        genres = metadata.get('genres', [])
        genres_str = ','.join(genres) if genres else ''
        logging.info(f"Genres from metadata: {genres_str}")

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
        # If it's a URL rather than a magnet link
        if magnet_link.startswith('http'):
            try:
                # For Jackett URLs or any other torrent URLs, download to temp file
                with tempfile.NamedTemporaryFile(delete=False, suffix='.torrent') as tmp:
                    response = requests.get(magnet_link, timeout=30)
                    response.raise_for_status()
                    tmp.write(response.content)
                    tmp.flush()
                    temp_file = tmp.name
                    logging.info("Downloaded torrent file")
            except Exception as e:
                error_message = str(e)
                logging.error(f"Failed to process torrent URL: {error_message}")
                if temp_file and os.path.exists(temp_file):
                    try:
                        os.unlink(temp_file)
                    except Exception as e:
                        logging.warning(f"Failed to delete temp file {temp_file}: {e}")
                return jsonify({'error': error_message}), 400

        # Add magnet/torrent to debrid provider
        debrid_provider = get_debrid_provider()
        try:
            torrent_id = debrid_provider.add_torrent(magnet_link, temp_file)
            logging.info(f"Torrent result: {torrent_id}")
            
            if not torrent_id:
                error_message = "Failed to add torrent to debrid provider"
                logging.error(error_message)
                return jsonify({'error': error_message}), 500

            # Extract torrent hash from magnet link or torrent file
            torrent_hash = None
            if magnet_link.startswith('magnet:'):
                # Extract hash from magnet link
                hash_match = re.search(r'btih:([a-fA-F0-9]{40})', magnet_link)
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
                    'version': version,
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
        if isinstance(debrid_provider, RealDebridProvider):
            # For Real Debrid, use the torrent ID directly
            torrent_info = debrid_provider.get_torrent_info(torrent_id)
            
            # Check if the torrent is cached or not
            is_cached = False
            if torrent_info:
                status = torrent_info.get('status', '')
                is_cached = status == 'downloaded'
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
                    # For TV shows, get episode-specific release date
                    metadata = get_metadata(tmdb_id=int(tmdb_id), item_media_type=media_type)
                    if metadata and metadata.get('seasons'):
                        season_data = metadata['seasons'].get(str(season_number), {})
                        episode_data = season_data.get('episodes', {}).get(str(episode_number), {})
                        release_date = episode_data.get('first_aired')
                        if release_date:
                            try:
                                # Parse the UTC datetime string
                                first_aired_utc = datetime.strptime(release_date, "%Y-%m-%dT%H:%M:%S.%fZ")
                                first_aired_utc = first_aired_utc.replace(tzinfo=timezone.utc)

                                # Convert UTC to local timezone
                                local_tz = _get_local_timezone()
                                local_dt = first_aired_utc.astimezone(local_tz)
                                
                                # Format the local date as string
                                release_date = local_dt.strftime("%Y-%m-%d")
                            except ValueError:
                                release_date = 'Unknown'
                else:
                    # For movies, get movie release date
                    metadata = get_metadata(tmdb_id=int(tmdb_id), item_media_type=media_type)
                    if metadata:
                        release_date = get_release_date(metadata, metadata.get('imdb_id'))
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
                    'version': version,
                    'tmdb_id': tmdb_id,
                    'imdb_id': imdb_id,
                    'state': 'Checking',
                    'filled_by_magnet': magnet_link,
                    'filled_by_torrent_id': torrent_id,
                    'filled_by_title': filled_by_title,
                    'filled_by_file': filled_by_file,
                    'release_date': release_date,
                    'genres': genres_str
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
                        season_data = metadata['seasons'].get(str(season_number), {})
                        episodes = season_data.get('episodes', {})
                        
                        # Create a MediaMatcher instance to find matching files
                        media_matcher = MediaMatcher()
                        
                        # For each episode in the season
                        for episode_num, episode_data in episodes.items():
                            try:
                                episode_num = int(episode_num)
                                # Create episode-specific item
                                episode_item = item.copy()
                                episode_item['episode_number'] = episode_num
                                
                                # Get episode-specific release date and title
                                first_aired = episode_data.get('first_aired')
                                episode_item['episode_title'] = episode_data.get('title', f'Episode {episode_num}')
                                
                                if first_aired:
                                    try:
                                        first_aired_utc = datetime.strptime(first_aired, "%Y-%m-%dT%H:%M:%S.%fZ")
                                        first_aired_utc = first_aired_utc.replace(tzinfo=timezone.utc)
                                        local_tz = _get_local_timezone()
                                        local_dt = first_aired_utc.astimezone(local_tz)
                                        
                                        # Format the local date as string
                                        episode_item['release_date'] = local_dt.strftime("%Y-%m-%d")
                                    except ValueError:
                                        episode_item['release_date'] = 'Unknown'
                                
                                # Find matching file for this episode
                                matches = media_matcher.match_content(files, episode_item)
                                if matches:
                                    episode_item['filled_by_file'] = matches[0][0]  # Use first match's path
                                    
                                    # Add episode to database
                                    from database import add_media_item
                                    episode_id = add_media_item(episode_item)
                                    if episode_id:
                                        episode_item['id'] = episode_id
                                        # Add to checking queue
                                        from queues.checking_queue import CheckingQueue
                                        checking_queue = CheckingQueue()
                                        checking_queue.add_item(episode_item)
                                        logging.info(f"Added episode {episode_num} to checking queue")
                                else:
                                    logging.warning(f"No matching file found for episode {episode_num}")
                            except Exception as e:
                                logging.error(f"Error processing episode {episode_num}: {str(e)}")
                                continue
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
    from web_scraper import get_available_versions

    versions = get_available_versions()
    is_requester = current_user.is_authenticated and current_user.role == 'requester'
    
    if request.method == 'GET':
        trendingMovies = trending_movies()
        if trendingMovies:
            return jsonify(trendingMovies)
        else:
            return jsonify({'error': 'Error retrieving trending movies'})
    return render_template('scraper.html', versions=versions, is_requester=is_requester)

@scraper_bp.route('/shows_trending', methods=['GET', 'POST'])
@user_required
@scraper_view_access_required
def shows_trending():
    from web_scraper import get_available_versions

    versions = get_available_versions()
    is_requester = current_user.is_authenticated and current_user.role == 'requester'
    
    if request.method == 'GET':
        trendingShows = trending_shows()
        if trendingShows:
            return jsonify(trendingShows)
        else:
            return jsonify({'error': 'Error retrieving trending shows'})
    return render_template('scraper.html', versions=versions, is_requester=is_requester)

@scraper_bp.route('/', methods=['GET', 'POST'])
@user_required
@scraper_view_access_required
@onboarding_required
def index():
    from web_scraper import get_available_versions, web_scrape

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
    from web_scraper import get_available_versions

    versions = get_available_versions()
    # Check if the user is a requester
    is_requester = current_user.is_authenticated and current_user.role == 'requester'
    
    if request.method == 'POST':
        media_id = request.form.get('media_id')
        title = request.form.get('title')
        year = request.form.get('year')
        
        if media_id:
            try:
                # Allow both requesters and regular users to get season data for browsing
                results = web_scrape_tvshow(media_id, title, year)
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
    from web_scraper import get_available_versions
    
    versions = get_available_versions()
    # Check if the user is a requester
    is_requester = current_user.is_authenticated and current_user.role == 'requester'
    
    if request.method == 'POST':
        media_id = request.form.get('media_id')
        season = request.form.get('season')
        title = request.form.get('title')
        year = request.form.get('year')
        
        if media_id:
            try:
                # Allow episode data to be retrieved for both requesters and regular users
                episodeResults = web_scrape_tvshow(media_id, title, year, season)
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
    try:
        # Check if the user is a requester and block the scraping action if true
        is_requester = current_user.is_authenticated and current_user.role == 'requester'
        if is_requester:
            return jsonify({
                'error': 'As a Requester, you can view content but cannot perform scraping actions.',
                'torrent_results': []  # Return empty results to avoid errors in the UI
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
        
        # Parse skip_cache_check parameter
        skip_cache_check = request.form.get('skip_cache_check', 'false').lower() == 'true'
        
        # Parse background_check parameter
        background_check = request.form.get('background_check', 'true').lower() == 'true'
        
        # Log the parameters
        logging.info(f"Select media: {media_id}, {title}, {year}, {media_type}, S{season or 'None'}E{episode or 'None'}, multi={multi}, version={version}")
        logging.info(f"Cache check settings: skip_cache_check={skip_cache_check}, background_check={background_check}")
        
        if not media_id or not title or not year or not media_type:
            return jsonify({'error': 'Missing required parameters'}), 400
            
        if season:
            season = int(season)
        if episode:
            episode = int(episode)
            
        # Parse genre_ids
        genres = []
        if genre_ids:
            try:
                genres = [int(g) for g in genre_ids.split(',') if g]
            except ValueError:
                logging.warning(f"Invalid genre_ids format: {genre_ids}")
                
        # Process the media selection
        result = process_media_selection(
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
        if isinstance(result, dict) and 'error' in result:
            return jsonify(result), 400
            
        # Return the results
        return jsonify(result)
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
            from web_scraper import parse_search_term
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
def get_item_details():
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
    
@scraper_bp.route('/run_scrape', methods=['POST'])
@user_required
@scraper_permission_required
def run_scrape():
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
        
        # Run first scrape with current settings
        original_results, _ = scrape(
            imdb_id, tmdb_id, title, year, media_type, version, season, episode, multi, genres, skip_cache_check
        )

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
            adjusted_results, _ = scrape(
                imdb_id, tmdb_id, title, year, media_type, version, season, episode, multi, genres, skip_cache_check
            )
        finally:
            # Revert settings back to original
            config = load_config()
            config['Scraping']['versions'][version] = original_version_settings
            save_config(config)

        # Ensure score_breakdown is included in the results
        for result in original_results + adjusted_results:
            if 'score_breakdown' not in result:
                result['score_breakdown'] = {'total_score': result.get('score', 0)}
            
            # Set default cache status to 'N/A'
            if 'cached' not in result:
                result['cached'] = 'N/A'
        
        # Check cache status for the first 5 results of each list
        if not skip_cache_check:
            try:
                debrid_provider = get_debrid_provider()
                if isinstance(debrid_provider, RealDebridProvider):
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

        return jsonify({
            'originalResults': original_results,
            'adjustedResults': adjusted_results
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
    try:
        # First get the IMDb ID from TMDB ID
        imdb_id, _ = DirectAPI.tmdb_to_imdb(str(tmdb_id), media_type='show')
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
        logging.info(f"Cache check request data: {data}")
        
        # Handle single item cache check (new approach)
        if 'index' in data:
            index = data.get('index')
            magnet_link = data.get('magnet_link')
            torrent_url = data.get('torrent_url')
            
            logging.info(f"Processing cache check for item at index {index}")
            if magnet_link:
                logging.info(f"Magnet link: {magnet_link[:60]}...")
            if torrent_url:
                logging.info(f"Torrent URL: {torrent_url[:60]}...")
                
            if not magnet_link and not torrent_url:
                logging.warning(f"No magnet link or torrent URL provided for index {index}")
                return jsonify({'status': 'check_unavailable'}), 200
            
            # Get the debrid provider
            debrid_provider = get_debrid_provider()
            logging.info(f"Using debrid provider: {debrid_provider.__class__.__name__}")
            
            # Create a torrent processor with the debrid provider
            torrent_processor = TorrentProcessor(debrid_provider)
            
            # Check cache status based on what we have
            if magnet_link:
                logging.info(f"Checking cache status for magnet link at index {index}")
                is_cached = torrent_processor.check_cache(magnet_link)
                logging.info(f"Cache check result for magnet link: {is_cached}")
            elif torrent_url:
                logging.info(f"Checking cache status for torrent URL at index {index}")
                is_cached = torrent_processor.check_cache_for_url(torrent_url)
                logging.info(f"Cache check result for torrent URL: {is_cached}")
            
            # Convert result to the expected format
            if is_cached is True:
                status = 'cached'
            elif is_cached is False:
                status = 'not_cached'
            else:
                # Handle None responses which can happen with download failures or empty torrents
                logging.warning(f"Cache check returned None for index {index}")
                status = 'check_unavailable'
                
            logging.info(f"Returning cache status for index {index}: {status}")
            return jsonify({'status': status, 'index': index}), 200
            
        # Handle multiple hashes (legacy approach)
        hashes = data.get('hashes', [])
        if not hashes:
            return jsonify({'error': 'No hashes provided'}), 400
            
        # Always limit to exactly 5 hashes, preserving the order
        if len(hashes) > 5:
            logging.info(f"Limiting cache check from {len(hashes)} to exactly 5 hashes")
            hashes = hashes[:5]
        elif len(hashes) < 5:
            logging.info(f"Only {len(hashes)} hashes provided, less than the target of 5")
            
        # Get the debrid provider and check its capabilities
        debrid_provider = get_debrid_provider()
        supports_cache_check = debrid_provider.supports_direct_cache_check
        supports_bulk_check = debrid_provider.supports_bulk_cache_checking
        
        # Check if this is a RealDebridProvider
        is_real_debrid = isinstance(debrid_provider, RealDebridProvider)
        
        # Check cache status for all hashes
        cache_status = {}
        if hashes:
            if supports_cache_check:
                try:
                    # Optimize for single hash requests which are common with our new frontend
                    if len(hashes) == 1:
                        hash_value = hashes[0]
                        is_cached = debrid_provider.is_cached(hash_value)
                        cache_status[hash_value] = is_cached
                        logging.info(f"Single hash cache status for {hash_value}: {is_cached}")
                    elif supports_bulk_check:
                        # If provider supports bulk checking, check all hashes at once
                        # But we need to ensure we maintain the order in the response
                        bulk_result = debrid_provider.is_cached(hashes)
                        if isinstance(bulk_result, bool):
                            # If we got a single boolean back, convert to dict
                            cache_status = {hash_value: bulk_result for hash_value in hashes}
                        else:
                            # Make sure we preserve the order of hashes in the response
                            cache_status = {}
                            for hash_value in hashes:
                                cache_status[hash_value] = bulk_result.get(hash_value, 'N/A')
                        logging.info(f"Bulk cache status from provider: {cache_status}")
                    else:
                        # Check hashes individually for providers that don't support bulk checking
                        # Process them in the exact order they were received
                        cache_status = {}
                        for hash_value in hashes:
                            try:
                                is_cached = debrid_provider.is_cached(hash_value)
                                cache_status[hash_value] = is_cached
                                logging.info(f"Individual cache status for {hash_value}: {is_cached}")
                            except Exception as e:
                                logging.error(f"Error checking individual cache status for {hash_value}: {e}")
                                cache_status[hash_value] = 'N/A'
                except Exception as e:
                    logging.error(f"Error checking cache status: {e}")
                    # Fall back to N/A on error
                    cache_status = {hash_value: 'N/A' for hash_value in hashes}
            else:
                # If provider doesn't support direct checking but is RealDebrid, check first 5 results
                if is_real_debrid:
                    logging.info("Using RealDebridProvider's is_cached method")
                    cache_status = {hash_value: 'N/A' for hash_value in hashes}  # Initialize all as N/A
                    torrent_ids_to_remove = []  # Track torrent IDs for removal
                    
                    # Check each hash in the order provided
                    for i, hash_value in enumerate(hashes):
                        try:
                            # Use the is_cached method which will add the torrent and return its cache status
                            # But we need to capture the torrent ID for later removal
                            magnet_link = f"magnet:?xt=urn:btih:{hash_value}"
                            cache_result = debrid_provider.is_cached(
                                magnet_link, 
                                result_title=f"Hash {hash_value}",
                                result_index=i
                            )
                            # Convert None to 'No' for frontend display
                            if cache_result is None:
                                cache_result = 'No'
                            cache_status[hash_value] = cache_result
                            logging.info(f"Cache status for hash {hash_value}: {cache_result}")
                            
                            # Try to find the torrent ID using the hash
                            torrent_id = debrid_provider._all_torrent_ids.get(hash_value)
                            if torrent_id:
                                torrent_ids_to_remove.append(torrent_id)
                                logging.info(f"Added torrent ID {torrent_id} to removal list")
                        except Exception as e:
                            logging.error(f"Error checking cache for hash {hash_value}: {str(e)}")
                    
                    # Remove all torrents after checking (even if they're cached)
                    for torrent_id in torrent_ids_to_remove:
                        try:
                            debrid_provider.remove_torrent(torrent_id, "Removed after cache check")
                            logging.info(f"Removed torrent with ID {torrent_id} after cache check")
                        except Exception as e:
                            logging.error(f"Error removing torrent {torrent_id}: {str(e)}")
                else:
                    # Mark all results as N/A if provider doesn't support direct checking
                    cache_status = {hash_value: 'N/A' for hash_value in hashes}
                    logging.info("Provider does not support direct cache checking, marking all as N/A")
        
        # Convert boolean values to strings for consistency with the frontend
        for hash_value, status in cache_status.items():
            if status is True:
                cache_status[hash_value] = 'Yes'
            elif status is False:
                cache_status[hash_value] = 'No'
                
        return jsonify({'cache_status': cache_status})
    except Exception as e:
        logging.error(f"Error in check_cache_status: {str(e)}", exc_info=True)
        return jsonify({'error': 'An error occurred while checking cache status'}), 500
