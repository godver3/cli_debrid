from flask import jsonify, request, render_template, session, Blueprint
import logging
from debrid.real_debrid import add_to_real_debrid
from .models import user_required, onboarding_required, admin_required
from settings import get_setting, get_all_settings, load_config, save_config
from database.database_reading import get_all_season_episode_counts
from web_scraper import trending_movies, trending_shows, web_scrape, web_scrape_tvshow, process_media_selection, process_torrent_selection
from web_scraper import get_media_details
from scraper.scraper import scrape
from utilities.manual_scrape import get_details
from web_scraper import search_trakt
from database.database_reading import get_all_season_episode_counts

scraper_bp = Blueprint('scraper', __name__)

@scraper_bp.route('/add_to_real_debrid', methods=['POST'])
def add_torrent_to_real_debrid():
    try:
        magnet_link = request.form.get('magnet_link')
        if not magnet_link:
            return jsonify({'error': 'No magnet link provided'}), 400

        result = add_to_real_debrid(magnet_link)
        logging.info(f"Torrent result: {result}")
        logging.info(f"Magnet link: {magnet_link}")
        if result:
            if isinstance(result, dict):
                status = result.get('status', '').lower()
                if status in ['downloading', 'queued']:
                    return jsonify({'message': f'Uncached torrent added to Real-Debrid successfully. Status: {status.capitalize()}'})
                elif status == 'magnet_error':
                    return jsonify({'error': 'Error processing magnet link'}), 400
                else:
                    return jsonify({'message': f'Torrent added to Real-Debrid successfully. Status: {status.capitalize()}'})
            else:
                return jsonify({'message': 'Cached torrent added to Real-Debrid successfully'})
        else:
            error_message = "No suitable video files found in the torrent."
            logging.error(f"Failed to add torrent to Real-Debrid: {error_message}")
            return jsonify({'error': error_message}), 500

    except Exception as e:
        error_message = str(e)
        logging.error(f"Error adding torrent to Real-Debrid: {error_message}")
        return jsonify({'error': f'An error occurred while adding to Real-Debrid: {error_message}'}), 500
    
@scraper_bp.route('/movies_trending', methods=['GET', 'POST'])
def movies_trending():
    from web_scraper import get_available_versions

    versions = get_available_versions()
    if request.method == 'GET':
        trendingMovies = trending_movies()
        if trendingMovies:
            return jsonify(trendingMovies)
        else:
            return jsonify({'error': 'Error restrieving Trakt Trending Movies'})
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
            return jsonify({'error': 'Error restrieving Trakt Trending Shows'})
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
            logging.info(f"Search results for '{search_term}': {results}")  # Log the results
            return jsonify({'results': results})  # Wrap results in a dictionary here
        else:
            return jsonify({'error': 'No search term provided'})
    
    return render_template('scraper.html', versions=versions)

@scraper_bp.route('/select_season', methods=['GET', 'POST'])
def select_season():
    from web_scraper import get_available_versions

    versions = get_available_versions()
    if request.method == 'POST':
        media_id = request.form.get('media_id')
        title = request.form.get('title')
        year = request.form.get('year')
        if media_id:
            results = web_scrape_tvshow(media_id, title, year)
            return jsonify(results)
        else:
            return jsonify({'error': 'No media_id provided'})
    
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
            episodeResults = web_scrape_tvshow(media_id, title, year, season)
            return jsonify(episodeResults)
        else:
            return jsonify({'error': 'No media_id provided'})
    
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
        genres = details.get('keywords', [])

        logging.info(f"Retrieved genres: {genres}")

        logging.info(f"Selecting media: {media_id}, {title}, {year}, {media_type}, S{season or 'None'}E{episode or 'None'}, multi={multi}, version={version}, genres={genres}")

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

        logging.info(f"Selecting media: {media_id}, {title}, {year}, {media_type}, S{season or 'None'}E{episode or 'None'}, multi={multi}, version={version}")

        torrent_results, cache_status = process_media_selection(media_id, title, year, media_type, season, episode, multi, version, genres)
        
        if not torrent_results:
            logging.warning("No torrent results found")
            return jsonify({'torrent_results': []})

        cached_results = []
        for result in torrent_results:
            # Check if the source is Jackett or Prowlarr
            if any(src in result.get('source', '').lower() for src in ['jackett', 'prowlarr']):
                result['cached'] = 'Not Checked'
            else:
                result_hash = result.get('hash')
                if result_hash:
                    is_cached = cache_status.get(result_hash, False)
                    result['cached'] = 'Yes' if is_cached else 'No'
                else:
                    result['cached'] = 'No'
            cached_results.append(result)

        logging.info(f"Processed {len(cached_results)} results")
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
    logging.debug(f"Received scrape data: {data}")
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

        logging.debug(f"Scraping with parameters: imdb_id={imdb_id}, tmdb_id={tmdb_id}, title={title}, year={year}, media_type={media_type}, version={version}, season={season}, episode={episode}, multi={multi}")

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

        logging.debug(f"Original version settings: {original_version_settings}")
        logging.debug(f"Modified version settings: {updated_version_settings}")

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