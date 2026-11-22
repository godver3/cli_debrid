"""Streaming Top 10 lists (JustWatch charts; routes keep their old /api/flixpatrol paths)."""

import logging
import requests
from datetime import datetime
from flask import jsonify, request
from utilities.settings import get_setting
from utilities.streaming_top10_api import get_available_platforms as get_flixpatrol_platforms, fetch_top10 as fetch_flixpatrol_top10, fetch_top10_weekly as fetch_flixpatrol_top10_weekly
from routes.models import user_required
from ._common import _FLIXPATROL_TTL, _FLIXPATROL_WEEKLY_TTL, _flixpatrol_cache, _flixpatrol_cache_lock, discover_bp
from .enrich import add_db_status_and_episode_info

@discover_bp.route('/api/flixpatrol/cache/clear', methods=['POST'])
@user_required
def flixpatrol_cache_clear():
    """Clear the FlixPatrol in-memory cache."""
    with _flixpatrol_cache_lock:
        _flixpatrol_cache.clear()
    return jsonify({'success': True, 'message': 'FlixPatrol cache cleared'})

@discover_bp.route('/api/flixpatrol/platforms')
@user_required
def flixpatrol_platforms():
    """
    Get available streaming platforms for Top 10.
    No API key required.
    """
    try:
        result = get_flixpatrol_platforms()
        return jsonify(result)
    except Exception as e:
        logging.error(f"Error getting FlixPatrol platforms: {e}")
        return jsonify({'error': str(e)}), 500

@discover_bp.route('/api/flixpatrol/top10/<platform>')
@user_required
def flixpatrol_top10(platform):
    """
    Get Top 10 content for a specific streaming platform.
    No API key required - JustWatch public streaming charts.

    Args:
        platform: Platform key (netflix, disney, amazon, hbo, apple, paramount, hulu, peacock)
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    try:
        media_type_filter = request.args.get('type', 'all')  # all, movie, tv

        # Serve from cache if fresh
        fp_cache_key = f'{platform}:{media_type_filter}'
        with _flixpatrol_cache_lock:
            fp_entry = _flixpatrol_cache.get(fp_cache_key)
        if fp_entry and datetime.now() < fp_entry['expires']:
            logging.debug(f"[FlixPatrol] Serving from cache ({fp_cache_key})")
            return jsonify({**fp_entry['data'], 'cached': True})

        # Fetch Top 10 from FlixPatrol
        result = fetch_flixpatrol_top10(platform, media_type=media_type_filter)

        if 'error' in result:
            return jsonify(result), 400

        items = result.get('items', [])

        # Enrich items with TMDB data and database status
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        enriched_items = []

        if tmdb_api_key and items:
            def enrich_item(item):
                """Helper function to enrich a single item with TMDB data"""
                title = item.get('title', '')
                media_type = item.get('media_type', 'movie')
                rank = item.get('rank')

                # Search TMDB by title directly (faster than FlixPatrol ID lookup)
                # JustWatch charts carry the TMDB ID; title search is only a fallback
                tmdb_id = item.get('tmdb_id')
                try:
                    # For unknown media_type (US platforms), try movie first then tv
                    search_types = ['movie', 'tv'] if media_type == 'unknown' else [media_type if media_type == 'tv' else 'movie']

                    for search_type in ([] if tmdb_id else search_types):
                        search_url = f"https://api.themoviedb.org/3/search/{search_type}?api_key={tmdb_api_key}&query={requests.utils.quote(title)}&page=1"
                        search_response = requests.get(search_url, timeout=5)
                        if search_response.status_code == 200:
                            search_data = search_response.json()
                            if search_data.get('results'):
                                tmdb_id = search_data['results'][0]['id']
                                media_type = search_type
                                break
                except Exception as e:
                    logging.debug(f"TMDB search failed for {title}: {e}")

                if not tmdb_id:
                    return {
                        'id': None,
                        'title': title,
                        'media_type': media_type if media_type != 'unknown' else 'movie',
                        'rank': rank,
                        'db_status': 'unknown',
                    }

                # Fetch TMDB details
                try:
                    if media_type == 'tv':
                        tmdb_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={tmdb_api_key}&language=en-US"
                    else:
                        tmdb_url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={tmdb_api_key}&language=en-US"

                    tmdb_response = requests.get(tmdb_url, timeout=5)
                    if tmdb_response.status_code == 200:
                        tmdb_data = tmdb_response.json()
                        # Extract genre IDs from genres array (TMDB details returns genres as objects)
                        genres = tmdb_data.get('genres', [])
                        genre_ids = [g['id'] for g in genres if isinstance(g, dict) and 'id' in g]
                        # Extract production company IDs
                        companies = tmdb_data.get('production_companies', [])
                        company_ids = [c['id'] for c in companies if isinstance(c, dict) and 'id' in c]
                        return {
                            'id': tmdb_id,
                            'title': tmdb_data.get('title') or tmdb_data.get('name'),
                            'overview': tmdb_data.get('overview', ''),
                            'poster_path': tmdb_data.get('poster_path'),
                            'backdrop_path': tmdb_data.get('backdrop_path'),
                            'release_date': tmdb_data.get('release_date') or tmdb_data.get('first_air_date', ''),
                            'vote_average': tmdb_data.get('vote_average', 0),
                            'vote_count': tmdb_data.get('vote_count', 0),
                            'media_type': media_type,
                            'genre_ids': genre_ids,
                            'original_language': tmdb_data.get('original_language', ''),
                            'origin_country': tmdb_data.get('origin_country', []),
                            'company_ids': company_ids,
                            'rank': rank,
                            '_tmdb_id': tmdb_id  # Keep for db status lookup
                        }
                except Exception as e:
                    logging.debug(f"TMDB details failed for {title}: {e}")

                return {
                    'id': tmdb_id,
                    'title': title,
                    'media_type': media_type,
                    'db_status': 'unknown',
                    'rank': rank,
                    '_tmdb_id': tmdb_id
                }

            # Use ThreadPoolExecutor for parallel TMDB requests
            with ThreadPoolExecutor(max_workers=10) as executor:
                # Use index as key to handle duplicate ranks (movies 1-10 + shows 1-10)
                future_to_idx = {executor.submit(enrich_item, item): idx for idx, item in enumerate(items)}
                results_map = {}

                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    try:
                        enriched = future.result()
                        results_map[idx] = enriched
                    except Exception as e:
                        logging.warning(f"Error enriching item: {e}")

                # Sort by original index to maintain order
                enriched_items = [results_map[k] for k in sorted(results_map.keys())]

            # Clean up internal fields and normalize IDs
            for item in enriched_items:
                # Move _tmdb_id to id if needed
                if '_tmdb_id' in item and not item.get('id'):
                    item['id'] = item['_tmdb_id']
                item.pop('_tmdb_id', None)

            # Add database status and episode info
            add_db_status_and_episode_info(enriched_items, use_battery=False)

        response_data = {
            'success': True,
            'platform': result.get('platform'),
            'platform_name': result.get('platform_name'),
            'date': result.get('date'),
            'results': enriched_items,
            'total_results': len(enriched_items)
        }

        # Only cache if we actually got results — don't poison cache with empty/error responses
        if enriched_items:
            with _flixpatrol_cache_lock:
                _flixpatrol_cache[fp_cache_key] = {
                    'data': response_data,
                    'expires': datetime.now() + _FLIXPATROL_TTL,
                }

        return jsonify(response_data)

    except Exception as e:
        logging.error(f"Error fetching FlixPatrol Top 10 for {platform}: {e}")
        return jsonify({'error': str(e)}), 500

@discover_bp.route('/api/flixpatrol/top10/<platform>/weekly')
@user_required
def flixpatrol_top10_weekly(platform):
    """
    Get this week's Top 10 for a platform.
    Cached for 24 hours since the data changes daily at most.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    try:
        media_type_filter = request.args.get('type', 'all')

        fp_cache_key = f'weekly:{platform}:{media_type_filter}'
        with _flixpatrol_cache_lock:
            fp_entry = _flixpatrol_cache.get(fp_cache_key)
        if fp_entry and datetime.now() < fp_entry['expires']:
            logging.debug(f"[FlixPatrol Weekly] Serving from cache ({fp_cache_key})")
            return jsonify({**fp_entry['data'], 'cached': True})

        result = fetch_flixpatrol_top10_weekly(platform, media_type=media_type_filter)

        if 'error' in result:
            return jsonify(result), 400

        items = result.get('items', [])
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        enriched_items = []

        if tmdb_api_key and items:
            def enrich_item(item):
                title = item.get('title', '')
                media_type = item.get('media_type', 'movie')
                rank = item.get('rank')
                # JustWatch charts carry the TMDB ID; title search is only a fallback
                tmdb_id = item.get('tmdb_id')
                try:
                    search_types = ['movie', 'tv'] if media_type == 'unknown' else [media_type if media_type == 'tv' else 'movie']
                    for search_type in ([] if tmdb_id else search_types):
                        sr = requests.get(
                            f"https://api.themoviedb.org/3/search/{search_type}?api_key={tmdb_api_key}&query={requests.utils.quote(title)}&page=1",
                            timeout=5)
                        if sr.ok and sr.json().get('results'):
                            tmdb_id = sr.json()['results'][0]['id']
                            media_type = search_type
                            break
                except Exception:
                    pass
                if not tmdb_id:
                    return {'id': None, 'title': title, 'media_type': media_type, 'rank': rank, 'db_status': 'unknown'}
                try:
                    ep = 'tv' if media_type == 'tv' else 'movie'
                    dr = requests.get(f"https://api.themoviedb.org/3/{ep}/{tmdb_id}?api_key={tmdb_api_key}&language=en-US", timeout=5)
                    if dr.ok:
                        d = dr.json()
                        return {
                            'id': tmdb_id,
                            'title': d.get('title') or d.get('name'),
                            'overview': d.get('overview', ''),
                            'poster_path': d.get('poster_path'),
                            'backdrop_path': d.get('backdrop_path'),
                            'release_date': d.get('release_date') or d.get('first_air_date', ''),
                            'vote_average': d.get('vote_average', 0),
                            'vote_count': d.get('vote_count', 0),
                            'media_type': media_type,
                            'genre_ids': [g['id'] for g in d.get('genres', [])],
                            'original_language': d.get('original_language', ''),
                            'origin_country': d.get('origin_country', []),
                            'company_ids': [c['id'] for c in d.get('production_companies', [])],
                            'rank': rank,
                            '_tmdb_id': tmdb_id,
                        }
                except Exception:
                    pass
                return {'id': tmdb_id, 'title': title, 'media_type': media_type, 'db_status': 'unknown', 'rank': rank, '_tmdb_id': tmdb_id}

            with ThreadPoolExecutor(max_workers=10) as executor:
                future_to_idx = {executor.submit(enrich_item, item): idx for idx, item in enumerate(items)}
                results_map = {}
                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    try:
                        results_map[idx] = future.result()
                    except Exception as e:
                        logging.warning(f"Error enriching weekly FlixPatrol item: {e}")
            enriched_items = [results_map[k] for k in sorted(results_map.keys())]
            for item in enriched_items:
                if '_tmdb_id' in item and not item.get('id'):
                    item['id'] = item['_tmdb_id']
                item.pop('_tmdb_id', None)
            add_db_status_and_episode_info(enriched_items, use_battery=False)

        response_data = {
            'success': True,
            'platform': result.get('platform'),
            'platform_name': result.get('platform_name'),
            'period': 'weekly',
            'date': result.get('date'),
            'results': enriched_items,
            'total_results': len(enriched_items),
        }
        if enriched_items:
            with _flixpatrol_cache_lock:
                _flixpatrol_cache[fp_cache_key] = {
                    'data': response_data,
                    'expires': datetime.now() + _FLIXPATROL_WEEKLY_TTL,
                }
        return jsonify(response_data)

    except Exception as e:
        logging.error(f"Error fetching FlixPatrol weekly Top 10 for {platform}: {e}")
        return jsonify({'error': str(e)}), 500
