"""Scraper page, live search, analytics and season/episode/media selection."""

from flask import jsonify, request, render_template, session
import copy
import logging
from ..models import user_required, onboarding_required, admin_required, scraper_view_access_required
from utilities.settings import get_setting
from database.database_reading import get_media_item_presence_overall, get_media_items_presence_batch
from utilities.web_scraper import web_scrape_tvshow, process_media_selection
from flask_login import current_user
import time
from utilities.tmdb_cache import get_cached_db_statuses
from ._common import _search_analytics, scraper_bp
from .trending import fetch_all_trending_parallel

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
    import inspect

    # Check if get_cached_db_statuses function exists
    try:
        from utilities.tmdb_cache import get_cached_db_statuses, get_cache_stats  # noqa: F401  (availability probe)
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
        season_raw = request.form.get('season')
        season = None
        if season_raw is not None:
            _s = str(season_raw).strip()
            if _s and _s.lower() not in ('null', 'none'):
                season = int(_s)
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
        
        logging.info(
            f"Select media: {media_id}, {title}, {year}, {media_type}, "
            f"S{season if season not in (None, '') else 'None'}E{episode if episode not in (None, '') else 'None'}, "
            f"multi={multi}, version={version}"
        )
        
        # Store imdb_id in session
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

        logging.info(f"Cache check settings: skip_cache_check={skip_cache_check}, background_check={background_check}")
        logging.debug(f"[select_media_route] Calling process_media_selection for '{title}'.")
        
        if not media_id or not title or not year or not media_type:
            return jsonify({'error': 'Missing required parameters'}), 400
            
        def _parse_opt_int(val):
            if val is None:
                return None
            s = str(val).strip()
            if not s or s.lower() in ('null', 'none'):
                return None
            return int(s)

        # Only a movie has no season: an empty/missing media_type keeps the numbers as before
        if media_type != 'movie':
            season = _parse_opt_int(season)
            episode = _parse_opt_int(episode)
        else:
            season = None
            episode = None
            
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
        if multi and season is not None and episode is None and media_type in ('tv', 'show'):
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
