"""Trending, TMDB show lists and recommendations."""

import logging
import requests
import threading
from datetime import datetime, timedelta
from flask import jsonify, request, redirect, url_for
from utilities.settings import get_setting
from utilities.recommendation_provider import get_available_recommendation_providers
from routes.models import user_required
from ._common import _TRENDING_TTL, _trending_cache, _trending_cache_lock, discover_bp
from .enrich import add_db_status_and_episode_info, enrich_with_digital_dates

@discover_bp.route('/api/trending')
@user_required
def trending():
    """
    Get trending content from TMDB
    Hybrid approach:
    - For anime=only: Uses discover endpoint with keyword filtering (210024)
    - For movies/shows: Uses trending/week endpoint for accurate weekly trending
    Frontend filters out anime from shows section
    """
    try:
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        if not tmdb_api_key:
            return jsonify({'error': 'TMDB API key not configured'}), 400

        # Get type filter parameter
        media_type = request.args.get('type', 'movie')  # movie or tv

        # Get anime filter parameter
        anime_filter = request.args.get('anime', 'include')  # include, exclude, only

        # Serve from cache if fresh
        cache_key = f'{media_type}:{anime_filter}'
        with _trending_cache_lock:
            entry = _trending_cache.get(cache_key)
        if entry and datetime.now() < entry['expires']:
            logging.debug(f"[Trending] Serving from cache ({cache_key})")
            return jsonify({'results': entry['data'], 'total_results': len(entry['data']), 'cached': True})
        stale_entry = entry  # keep reference to serve as fallback on TMDB failure

        results = []

        # For anime-only requests, use discover endpoint with keyword filtering
        if anime_filter == 'only':
            # TMDB keyword ID for anime
            ANIME_KEYWORD_ID = '210024'

            # Use discover endpoint for anime (only supports TV)
            url = f"https://api.themoviedb.org/3/discover/tv?api_key={tmdb_api_key}&sort_by=popularity.desc&page=1&with_keywords={ANIME_KEYWORD_ID}"

            response = requests.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()

            # Process TV results
            for tv in data.get('results', [])[:20]:
                results.append({
                    'id': tv['id'],
                    'name': tv['name'],
                    'overview': tv.get('overview', ''),
                    'poster_path': tv.get('poster_path'),
                    'backdrop_path': tv.get('backdrop_path'),
                    'first_air_date': tv.get('first_air_date', ''),
                    'vote_average': tv.get('vote_average', 0),
                    'vote_count': tv.get('vote_count', 0),
                    'media_type': 'tv',
                    'genre_ids': tv.get('genre_ids', [])
                })
        else:
            # For movies and shows, use original trending/week endpoint
            if media_type == 'movie':
                base_url = f"https://api.themoviedb.org/3/trending/movie/week?api_key={tmdb_api_key}"
            else:  # tv
                base_url = f"https://api.themoviedb.org/3/trending/tv/week?api_key={tmdb_api_key}"

            # For TV shows, fetch 2 pages to get 35+ results (20 per page)
            # For movies, fetch 1 page to get 20 results
            all_items = []
            pages_to_fetch = 2 if media_type == 'tv' else 1

            for page in range(1, pages_to_fetch + 1):
                url = f"{base_url}&page={page}"
                response = requests.get(url, timeout=10)
                response.raise_for_status()
                data = response.json()
                all_items.extend(data.get('results', []))

            # Process results
            # For TV shows, limit to 35 results; for movies, limit to 20
            limit = 35 if media_type == 'tv' else 20
            for item in all_items[:limit]:
                if media_type == 'movie':
                    results.append({
                        'id': item['id'],
                        'title': item['title'],
                        'overview': item.get('overview', ''),
                        'poster_path': item.get('poster_path'),
                        'backdrop_path': item.get('backdrop_path'),
                        'release_date': item.get('release_date', ''),
                        'vote_average': item.get('vote_average', 0),
                        'vote_count': item.get('vote_count', 0),
                        'media_type': 'movie',
                        'genre_ids': item.get('genre_ids', [])
                    })
                else:  # tv
                    results.append({
                        'id': item['id'],
                        'name': item['name'],
                        'overview': item.get('overview', ''),
                        'poster_path': item.get('poster_path'),
                        'backdrop_path': item.get('backdrop_path'),
                        'first_air_date': item.get('first_air_date', ''),
                        'vote_average': item.get('vote_average', 0),
                        'vote_count': item.get('vote_count', 0),
                        'media_type': 'tv',
                        'genre_ids': item.get('genre_ids', [])
                    })

        # Enrich with digital release dates
        results = enrich_with_digital_dates(results, 'all', tmdb_api_key)

        # Add database status and episode info (skip Battery for faster discovery browsing)
        add_db_status_and_episode_info(results, use_battery=False)

        logging.info(f"Trending: {media_type} with anime={anime_filter}, returned {len(results)} results")

        # Store in cache
        with _trending_cache_lock:
            _trending_cache[cache_key] = {
                'data': results,
                'expires': datetime.now() + _TRENDING_TTL,
            }

        return jsonify({
            'results': results,
            'total_results': len(results)
        })

    except (requests.exceptions.RequestException, ValueError) as e:
        logging.error(f"TMDB trending API error: {e}")
        if stale_entry:
            logging.warning(f"[Trending] Serving stale cache for {cache_key} due to TMDB error")
            return jsonify({'results': stale_entry['data'], 'total_results': len(stale_entry['data']), 'cached': True, 'stale': True})
        return jsonify({'error': 'Failed to fetch trending content'}), 503
    except Exception as e:
        logging.error(f"Discover trending error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

_tmdb_shows_cache_lock = threading.Lock()

_tmdb_shows_cache = {}

_TMDB_SHOWS_TTL = timedelta(hours=6)

@discover_bp.route('/api/tmdb/shows/<list_type>')
@user_required
def tmdb_shows(list_type):
    """
    Fetch TMDB show lists: popular, top_rated, airing_today, trending.
    Returns enriched results matching the discover card format.
    """
    VALID_TYPES = {
        'popular':      'tv/popular',
        'top_rated':    'tv/top_rated',
        'airing_today': 'tv/airing_today',
        'trending':     'trending/tv/week',
    }
    if list_type not in VALID_TYPES:
        return jsonify({'error': f'Unknown list type: {list_type}'}), 400

    tmdb_api_key = get_setting('TMDB', 'api_key', '')
    if not tmdb_api_key:
        return jsonify({'error': 'TMDB API key not configured'}), 400

    with _tmdb_shows_cache_lock:
        entry = _tmdb_shows_cache.get(list_type)
    if entry and datetime.now() < entry['expires']:
        return jsonify({**entry['data'], 'cached': True})

    try:
        endpoint = VALID_TYPES[list_type]
        results = []
        for page in range(1, 3):  # 2 pages = up to 40 results
            url = f"https://api.themoviedb.org/3/{endpoint}?api_key={tmdb_api_key}&language=en-US&page={page}"
            r = requests.get(url, timeout=10)
            if not r.ok:
                break
            for item in r.json().get('results', []):
                results.append({
                    'id': item['id'],
                    'title': item.get('name') or item.get('title', ''),
                    'name': item.get('name') or item.get('title', ''),
                    'overview': item.get('overview', ''),
                    'poster_path': item.get('poster_path'),
                    'backdrop_path': item.get('backdrop_path'),
                    'release_date': item.get('first_air_date', ''),
                    'first_air_date': item.get('first_air_date', ''),
                    'vote_average': item.get('vote_average', 0),
                    'vote_count': item.get('vote_count', 0),
                    'media_type': 'tv',
                    'genre_ids': item.get('genre_ids', []),
                    'original_language': item.get('original_language', ''),
                    'origin_country': item.get('origin_country', []),
                    'popularity': item.get('popularity', 0),
                })

        add_db_status_and_episode_info(results, use_battery=False)

        response_data = {'success': True, 'results': results, 'total_results': len(results), 'list_type': list_type}
        with _tmdb_shows_cache_lock:
            _tmdb_shows_cache[list_type] = {'data': response_data, 'expires': datetime.now() + _TMDB_SHOWS_TTL}

        return jsonify(response_data)

    except Exception as e:
        logging.error(f"TMDB shows {list_type} error: {e}")
        return jsonify({'error': str(e)}), 500

@discover_bp.route('/api/recommendations')
@user_required
def recommendations():
    """Recommendations from one connected provider.

    ``source`` picks trakt, scrob or library; without it (or if it isn't
    connected) the default is used: Trakt, then Scrob's ``For You``, then
    recommendations built from the watch history and collection via TMDB.
    ``viewer`` picks whose history drives the library ones. Content-source
    entries are intentionally irrelevant here; they control automatic Wanted
    ingestion, while this endpoint only displays recommendations.
    """
    media_type = request.args.get('type', 'all')
    if media_type not in ('all', 'movie', 'tv'):
        media_type = 'all'

    available = get_available_recommendation_providers()
    provider = request.args.get('source')
    if provider not in available:
        provider = available[0] if available else None
    if provider == 'trakt':
        return redirect(url_for(
            '.trakt_special_list',
            list_type='recommendations',
            type=media_type,
        ))
    if provider == 'scrob':
        return redirect(url_for(
            '.scrob_special_list',
            list_type='For You',
            type=media_type,
        ))
    if provider == 'library':
        try:
            from utilities.library_recommendations import get_library_recommendations
            results = get_library_recommendations(request.args.get('viewer') or None)
        except Exception as e:
            logging.error(f"Library recommendations error: {e}")
            return jsonify({'success': False, 'results': [], 'error': str(e)}), 500
        if media_type != 'all':
            results = [r for r in results if r['media_type'] == media_type]
        add_db_status_and_episode_info(results, use_battery=False)
        return jsonify({
            'success': True,
            'provider': 'library',
            'results': results,
            'total_results': len(results),
        })

    return jsonify({
        'success': False,
        'results': [],
        'total_results': 0,
        'error': 'No recommendation provider configured',
    })

@discover_bp.route('/api/recommendations/sources')
@user_required
def recommendation_sources():
    """What the Recommendations dropdown offers.

    ``sources`` lists the connected providers in default order. The library
    entry carries the synced Plex/Emby/Jellyfin users (empty until the viewer
    history task has run) and its own name: the media server it learns from.
    """
    sources = []
    for provider in get_available_recommendation_providers():
        if provider == 'trakt':
            sources.append({'id': 'trakt', 'name': 'Trakt'})
        elif provider == 'scrob':
            sources.append({'id': 'scrob', 'name': 'Scrob'})
        else:
            from utilities.viewer_history import get_viewers, main_viewer_id
            try:
                viewers = get_viewers()
            except Exception as e:
                logging.error(f"Recommendation viewers error: {e}")
                viewers = []
            if get_setting('Plex', 'url', ''):
                name = 'Plex'
            elif viewers:
                name = {'jellyfin': 'Jellyfin', 'emby': 'Emby', 'silo': 'Silo'}.get(viewers[0]['source'], 'Library')
            else:
                name = 'Library'
            sources.append({
                'id': 'library',
                'name': name,
                'viewers': viewers,
                'default_viewer': main_viewer_id() if viewers else 'main',
            })
    return jsonify({'success': True, 'sources': sources, 'default': sources[0]['id'] if sources else None})
