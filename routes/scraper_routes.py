from flask import jsonify, request, render_template, session, Blueprint
import logging
from debrid import get_debrid_provider
from debrid.real_debrid.client import RealDebridProvider
from .models import user_required, onboarding_required, admin_required
from settings import get_setting, get_all_settings, load_config, save_config
from database.database_reading import get_all_season_episode_counts
from web_scraper import trending_movies, trending_shows, web_scrape, web_scrape_tvshow, process_media_selection, process_torrent_selection
from web_scraper import get_media_details
from scraper.scraper import scrape
from utilities.manual_scrape import get_details
from web_scraper import search_trakt
from database.database_reading import get_all_season_episode_counts
from metadata.metadata import get_imdb_id_if_missing
import re
from queues.media_matcher import MediaMatcher
from guessit import guessit
from typing import Dict, Any, Tuple
import requests
import tempfile
import os
import bencodepy
from debrid.common.torrent import torrent_to_magnet
import hashlib

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
def add_torrent_to_debrid():
    try:
        magnet_link = request.form.get('magnet_link')
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
        else:
            # For TorBox, extract and use the hash
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

        # Handle different debrid statuses
        status = torrent_info.get('status', '').lower()
        if status in ['downloading', 'queued']:
            return jsonify({'message': f'Uncached torrent added successfully. Status: {status.capitalize()}'})
        elif status == 'magnet_error':
            return jsonify({'error': 'Error processing magnet link'}), 400
        else:
            return jsonify({'message': f'Torrent added successfully. Status: {status.capitalize()}'})

    except Exception as e:
        error_message = str(e)
        logging.error(f"Error adding torrent: {error_message}")
        return jsonify({'error': f'An error occurred while adding torrent: {error_message}'}), 500

@scraper_bp.route('/movies_trending', methods=['GET', 'POST'])
def movies_trending():
    from web_scraper import get_available_versions

    versions = get_available_versions()
    if request.method == 'GET':
        trendingMovies = trending_movies()
        if trendingMovies:
            return jsonify(trendingMovies)
        else:
            return jsonify({'error': 'Error retrieving trending movies'})
    return render_template('scraper.html', versions=versions)

@scraper_bp.route('/shows_trending', methods=['GET', 'POST'])
def shows_trending():
    from web_scraper import get_available_versions

    versions = get_available_versions()
    if request.method == 'GET':
        trendingShows = trending_shows()
        if trendingShows:
            return jsonify(trendingShows)
        else:
            return jsonify({'error': 'Error retrieving trending shows'})
    return render_template('scraper.html', versions=versions)

@scraper_bp.route('/', methods=['GET', 'POST'])
@user_required
@onboarding_required
def index():
    from web_scraper import get_available_versions, web_scrape

    versions = get_available_versions()
    if request.method == 'POST':
        search_term = request.form.get('search_term')
        version = request.form.get('version')
        if search_term:
            session['search_term'] = search_term  # Store the search term in the session
            session['version'] = version  # Store the version in the session
            results = web_scrape(search_term, version)
            return jsonify({'results': results})  # Wrap results in a dictionary here
        else:
            return jsonify({'error': 'No search term provided'})
        # Check if TMDB API key is set
    tmdb_api_key = get_setting('TMDB', 'api_key', '')
    tmdb_api_key_set = bool(tmdb_api_key)
    return render_template('scraper.html', versions=versions, tmdb_api_key_set=tmdb_api_key_set)

@scraper_bp.route('/select_season', methods=['GET', 'POST'])
def select_season():
    from web_scraper import get_available_versions

    versions = get_available_versions()
    if request.method == 'POST':
        media_id = request.form.get('media_id')
        title = request.form.get('title')
        year = request.form.get('year')
        
        if media_id:
            try:
                results = web_scrape_tvshow(media_id, title, year)
                if not results:
                    return jsonify({'error': 'No results found'}), 404
                    
                session['show_results'] = results
                return jsonify(results)
            except Exception as e:
                logging.error(f"Error in select_season: {str(e)}", exc_info=True)
                return jsonify({'error': str(e)}), 500
        else:
            return jsonify({'error': 'No media_id provided'}), 400
    
    return render_template('scraper.html', versions=versions)

@scraper_bp.route('/select_episode', methods=['GET', 'POST'])
def select_episode():
    from web_scraper import get_available_versions
    
    versions = get_available_versions()
    if request.method == 'POST':
        media_id = request.form.get('media_id')
        season = request.form.get('season')
        title = request.form.get('title')
        year = request.form.get('year')
        
        if media_id:
            try:
                episodeResults = web_scrape_tvshow(media_id, title, year, season)
                if not episodeResults:
                    return jsonify({'error': 'No results found'}), 404
                    
                # Ensure each episode has required fields
                if 'results' in episodeResults:
                    for episode in episodeResults['results']:
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
    
    return render_template('scraper.html', versions=versions)

@scraper_bp.route('/select_media', methods=['POST'])
def select_media():
    try:
        media_id = request.form.get('media_id')
        title = request.form.get('title')
        year = request.form.get('year')
        media_type = request.form.get('media_type')
        season = request.form.get('season')
        episode = request.form.get('episode')
        multi = request.form.get('multi', 'false').lower() in ['true', '1', 'yes', 'on']
        version = request.form.get('version')

        # Fetch detailed information from Overseerr
        details = get_media_details(media_id, media_type)

        # Extract keywords and genres
        genres = details.get('genres', [])

        if not version or version == 'undefined':
            version = get_setting('Scraping', 'default_version', '1080p')  # Fallback to a default version

        season = int(season) if season and season.isdigit() else None
        episode = int(episode) if episode and episode.isdigit() else None

        # Adjust multi and episode based on season
        if media_type == 'tv' and season is not None:
            if episode is None:
                episode = 1
                multi = True
            else:
                multi = False

        torrent_results, cache_status = process_media_selection(media_id, title, year, media_type, season, episode, multi, version, genres)
        
        if not torrent_results:
            return jsonify({'torrent_results': []})

        cached_results = []
        for result in torrent_results:
            # Cache status should already be set by process_media_selection
            if 'cached' not in result:
                result['cached'] = 'N/A'  # Fallback if somehow not set
            cached_results.append(result)

        return jsonify({'torrent_results': cached_results})
    except Exception as e:
        logging.error(f"Error in select_media: {str(e)}", exc_info=True)
        return jsonify({'error': 'An error occurred while selecting media'}), 500

@scraper_bp.route('/add_torrent', methods=['POST'])
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
            search_results = search_trakt(search_term)
            
            # Fetch IMDB IDs and season/episode counts for each result
            for result in search_results:
                details = get_details(result)
                
                if details:
                    imdb_id = details.get('externalIds', {}).get('imdbId', 'N/A')
                    tmdb_id = details.get('id', 'N/A')
                    result['imdbId'] = imdb_id
                    
                    if result['mediaType'] == 'tv':
                        overseerr_url = get_setting('Overseerr', 'url')
                        overseerr_api_key = get_setting('Overseerr', 'api_key')
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
            imdb_id, tmdb_id, title, year, media_type, version, season, episode, multi, genres
        )

        # Update version settings with modified settings
        updated_version_settings = original_version_settings.copy()
        updated_version_settings.update(modified_settings)

        # Save modified settings temporarily
        config['Scraping']['versions'][version] = updated_version_settings
        save_config(config)

        # Run second scrape with modified settings
        try:
            adjusted_results, _ = scrape(
                imdb_id, tmdb_id, title, year, media_type, version, season, episode, multi, genres
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

        return jsonify({
            'originalResults': original_results,
            'adjustedResults': adjusted_results
        })
    except Exception as e:
        logging.error(f"Error in run_scrape: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500