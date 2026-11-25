"""Add-torrent, battery search, scraper tester, item details, scrape runs, image proxy."""

from flask import jsonify, request, render_template, session
import logging
from debrid import get_debrid_provider
from ..models import user_required, onboarding_required, admin_required, scraper_permission_required
from utilities.settings import get_all_settings, load_config, save_config
from database.database_reading import get_all_season_episode_counts
from utilities.web_scraper import process_torrent_selection
from scraper.scraper import scrape
from utilities.manual_scrape import get_details
from utilities.web_scraper import search_trakt
import os
import re
from ._common import scraper_bp

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
        # TVDB ranks e.g. Parasite (2019) outside its top 50 for "Parasite", so
        # when the query ends in a year ("Parasite 2019" / "Parasite (2019)")
        # also search the title filtered to that year and list those first.
        # The plain search stays in so "Blade Runner 2049" still works.
        year_match = re.match(r'^(.+?)\s*\(?((?:19|20)\d{2})\)?$', query)
        if year_match:
            base_title = year_match.group(1).strip()
            year_results, year_source = DirectAPI.search_media(
                base_title, year=int(year_match.group(2)))
            if year_results:
                # Exact title matches first (TVDB puts "Paradise ..." above Parasite)
                year_results.sort(key=lambda r: base_title.lower() not in (
                    (r.get('title') or '').lower(), (r.get('original_title') or '').lower()))
                seen = set()
                merged = []
                for r in year_results + (results or []):
                    key = (r.get('type'), r.get('imdb_id') or r.get('tmdb_id') or r.get('title'), r.get('year'))
                    if key not in seen:
                        seen.add(key)
                        merged.append(r)
                results, source = merged, source or year_source
        if not results:
            return jsonify({'results': []})
        normalized = []
        for r in results:
            normalized.append({
                'id': r.get('tmdb_id'),
                'tmdb_id': r.get('tmdb_id'),
                'imdb_id': r.get('imdb_id'),
                'title': r.get('title'),
                'original_title': r.get('original_title'),
                'year': r.get('year'),
                'media_type': 'tv' if r.get('type') == 'show' else r.get('type', 'movie'),
            })
        return jsonify({'results': normalized, 'source': source})
    except Exception as e:
        logging.error(f"Battery search error: {e}", exc_info=True)
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
