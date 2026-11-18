"""Trending movies/shows/anime with a background-refreshed cache."""

from flask import jsonify, request, render_template
import copy
import logging
from ..models import user_required, scraper_view_access_required
from database.database_reading import get_media_item_presence_overall, get_media_items_presence_batch
from utilities.web_scraper import trending_movies, trending_shows, trending_anime
from flask_login import current_user
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from ._common import scraper_bp

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

# Last trending result, served immediately while a refresh runs in the
# background once it is older than _TRENDING_FRESH_SECONDS. The page used to
# wait for Trakt + up to 60 TMDB lookups whenever the 60 s cache had expired.
_TRENDING_FRESH_SECONDS = 60

_trending_state = {'data': None, 'fetched_at': 0.0, 'refreshing': False}

_trending_lock = threading.Lock()

def _fetch_all_trending_now():
    """Fetch trending movies, shows and anime in parallel (10 s budget)."""
    start_time = time.time()
    results = {
        'trendingMovies': [],
        'trendingShows': [],
        'trendingAnime': []
    }

    executor = ThreadPoolExecutor(max_workers=3)
    try:
        futures = {
            'movies': executor.submit(trending_movies),
            'shows': executor.submit(trending_shows),
            'anime': executor.submit(trending_anime)
        }

        for key, future in futures.items():
            try:
                data = future.result(timeout=max(0.1, 10 - (time.time() - start_time)))
                if key == 'movies' and data and 'trendingMovies' in data:
                    results['trendingMovies'] = data['trendingMovies']
                elif key == 'shows' and data and 'trendingShows' in data:
                    results['trendingShows'] = data['trendingShows']
                elif key == 'anime' and data and 'trendingAnime' in data:
                    results['trendingAnime'] = data['trendingAnime']
            except Exception as e:
                logging.error(f"Error fetching trending {key}: {e}")
    finally:
        # Don't wait for a worker that blew the budget (a `with` block would).
        executor.shutdown(wait=False)

    duration = (time.time() - start_time) * 1000
    logging.info(f"✅ Fetched all trending in parallel: {duration:.0f}ms")
    return results

def _refresh_trending_in_background():
    try:
        data = _fetch_all_trending_now()
        with _trending_lock:
            _trending_state['data'] = data
            _trending_state['fetched_at'] = time.time()
    except Exception as e:
        logging.error(f"Background trending refresh failed: {e}")
    finally:
        with _trending_lock:
            _trending_state['refreshing'] = False

def fetch_all_trending_parallel():
    """
    Trending movies, shows and anime for the scraper page.

    Only the very first call waits for the fetch; later calls get the last
    result straight away and, if it is over a minute old, start one refresh
    in the background. Callers must not modify the returned dict.
    """
    with _trending_lock:
        data = _trending_state['data']
        age = time.time() - _trending_state['fetched_at']
        if data is not None:
            if age >= _TRENDING_FRESH_SECONDS and not _trending_state['refreshing']:
                _trending_state['refreshing'] = True
                threading.Thread(target=_refresh_trending_in_background, name='trending-refresh', daemon=True).start()
            return data

    logging.info("📡 No trending data yet: fetching in parallel...")
    data = _fetch_all_trending_now()
    with _trending_lock:
        _trending_state['data'] = data
        _trending_state['fetched_at'] = time.time()
    return data

@scraper_bp.route('/all_trending', methods=['GET'])
@user_required
@scraper_view_access_required
def all_trending():
    """Combined endpoint that returns all trending data in a single request - NOW WITH PARALLEL FETCHING"""
    from routes.poster_cache import cache_trending_response

    try:
        # OPTIMIZATION: Use parallel fetching with caching
        # Deep copy: db_status is written into the items below.
        trending_data = copy.deepcopy(fetch_all_trending_parallel())

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
