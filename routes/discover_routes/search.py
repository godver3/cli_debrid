"""Discover page, search, people, filmography and batch requests."""

import logging
import requests
from flask import render_template, jsonify, request
from flask_login import current_user
from utilities.tmdb_cache import get_tmdb_json
from utilities.settings import get_setting
from routes.models import user_required
from ._common import discover_bp
from .enrich import add_db_status_and_episode_info, enrich_with_digital_dates

@discover_bp.route('/')
@user_required
def index():
    """Main discover page"""
    from utilities.web_scraper import get_available_versions
    from ..utils import is_user_system_enabled

    tmdb_api_key = get_setting('TMDB', 'api_key', '')
    versions = get_available_versions()

    # Check permissions
    if not is_user_system_enabled():
        has_admin_permissions = True
    else:
        has_admin_permissions = current_user.is_authenticated and current_user.role == 'admin'

    return render_template('discover.html',
                         tmdb_configured=bool(tmdb_api_key),
                         versions=versions,
                         has_admin_permissions=has_admin_permissions)

@discover_bp.route('/api/search')
@user_required
def search():
    """
    Search discover content using TMDB API
    Supports text search, IMDb IDs (tt1234567), and TMDB IDs (tmdb:12345 or just 12345)
    No caching to ensure fresh results for each search
    """
    import re
    try:
        # Get parameters
        query = request.args.get('query', '').strip()
        media_type = request.args.get('type', 'all')  # all, movie, tv
        page = int(request.args.get('page', 1))
        sort_by = request.args.get('sort_by', 'relevance')  # relevance, popularity, rating

        # Get TMDB API key
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        if not tmdb_api_key:
            return jsonify({'error': 'TMDB API key not configured'}), 400

        results = []
        movie_data = {'total_pages': 1}
        tv_data = {'total_pages': 1}

        # Check for IMDb ID (tt followed by digits)
        imdb_match = re.match(r'^(tt\d+)$', query, re.IGNORECASE)
        if imdb_match:
            imdb_id = imdb_match.group(1).lower()
            logging.info(f"[Discover] IMDb ID search: {imdb_id}")

            # Use TMDB's find endpoint to look up by IMDb ID
            find_url = f"https://api.themoviedb.org/3/find/{imdb_id}?api_key={tmdb_api_key}&external_source=imdb_id"
            find_response = requests.get(find_url, timeout=10)
            find_response.raise_for_status()
            find_data = find_response.json()

            # Process movie results
            if media_type in ['all', 'movie']:
                for idx, movie in enumerate(find_data.get('movie_results', [])):
                    results.append({
                        'id': movie['id'],
                        'title': movie.get('title', ''),
                        'overview': movie.get('overview', ''),
                        'poster_path': movie.get('poster_path'),
                        'backdrop_path': movie.get('backdrop_path'),
                        'release_date': movie.get('release_date', ''),
                        'vote_average': movie.get('vote_average', 0),
                        'vote_count': movie.get('vote_count', 0),
                        'popularity': movie.get('popularity', 0),
                        'relevance_order': idx,
                        'media_type': 'movie',
                        'genre_ids': movie.get('genre_ids', []),
                        'imdb_id': imdb_id
                    })

            # Process TV results
            if media_type in ['all', 'tv']:
                for idx, tv in enumerate(find_data.get('tv_results', [])):
                    results.append({
                        'id': tv['id'],
                        'name': tv.get('name', ''),
                        'title': tv.get('name', ''),
                        'overview': tv.get('overview', ''),
                        'poster_path': tv.get('poster_path'),
                        'backdrop_path': tv.get('backdrop_path'),
                        'first_air_date': tv.get('first_air_date', ''),
                        'release_date': tv.get('first_air_date', ''),
                        'vote_average': tv.get('vote_average', 0),
                        'vote_count': tv.get('vote_count', 0),
                        'popularity': tv.get('popularity', 0),
                        'relevance_order': len(results),
                        'media_type': 'tv',
                        'genre_ids': tv.get('genre_ids', []),
                        'imdb_id': imdb_id
                    })

            # Add database status and episode info
            add_db_status_and_episode_info(results, use_battery=False)

            return jsonify({
                'results': results,
                'page': 1,
                'total_pages': 1,
                'total_results': len(results),
                'search_type': 'imdb_id'
            })

        # Check for TMDB ID (tmdb: prefix or just digits)
        tmdb_match = re.match(r'^tmdb:?(\d+)$', query, re.IGNORECASE)
        if not tmdb_match:
            # Check for plain digits (treat as TMDB ID)
            digits_match = re.match(r'^(\d+)$', query)
            if digits_match and int(digits_match.group(1)) > 0:
                tmdb_match = digits_match

        if tmdb_match:
            tmdb_id = tmdb_match.group(1)
            logging.info(f"[Discover] TMDB ID search: {tmdb_id}")

            # Try to fetch both movie and TV with this ID
            if media_type in ['all', 'movie']:
                try:
                    movie_url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={tmdb_api_key}&language=en-US"
                    movie_response = requests.get(movie_url, timeout=10)
                    if movie_response.status_code == 200:
                        movie = movie_response.json()
                        results.append({
                            'id': movie['id'],
                            'title': movie.get('title', ''),
                            'overview': movie.get('overview', ''),
                            'poster_path': movie.get('poster_path'),
                            'backdrop_path': movie.get('backdrop_path'),
                            'release_date': movie.get('release_date', ''),
                            'vote_average': movie.get('vote_average', 0),
                            'vote_count': movie.get('vote_count', 0),
                            'popularity': movie.get('popularity', 0),
                            'relevance_order': 0,
                            'media_type': 'movie',
                            'genre_ids': [g['id'] for g in movie.get('genres', [])],
                            'imdb_id': movie.get('imdb_id')
                        })
                except Exception as e:
                    logging.debug(f"TMDB movie lookup failed for {tmdb_id}: {e}")

            if media_type in ['all', 'tv']:
                try:
                    tv_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={tmdb_api_key}&language=en-US"
                    tv_response = requests.get(tv_url, timeout=10)
                    if tv_response.status_code == 200:
                        tv = tv_response.json()
                        results.append({
                            'id': tv['id'],
                            'name': tv.get('name', ''),
                            'title': tv.get('name', ''),
                            'overview': tv.get('overview', ''),
                            'poster_path': tv.get('poster_path'),
                            'backdrop_path': tv.get('backdrop_path'),
                            'first_air_date': tv.get('first_air_date', ''),
                            'release_date': tv.get('first_air_date', ''),
                            'vote_average': tv.get('vote_average', 0),
                            'vote_count': tv.get('vote_count', 0),
                            'popularity': tv.get('popularity', 0),
                            'relevance_order': len(results),
                            'media_type': 'tv',
                            'genre_ids': [g['id'] for g in tv.get('genres', [])]
                        })
                except Exception as e:
                    logging.debug(f"TMDB TV lookup failed for {tmdb_id}: {e}")

            # Add database status and episode info
            add_db_status_and_episode_info(results, use_battery=False)

            return jsonify({
                'results': results,
                'page': 1,
                'total_pages': 1,
                'total_results': len(results),
                'search_type': 'tmdb_id'
            })

        # Standard text search
        # Search movies if requested
        if media_type in ['all', 'movie']:
            movie_url = f"https://api.themoviedb.org/3/search/movie?api_key={tmdb_api_key}&query={query}&page={page}&language=en-US"
            movie_response = requests.get(movie_url, timeout=10)
            movie_response.raise_for_status()
            movie_data = movie_response.json()

            for idx, movie in enumerate(movie_data.get('results', [])):
                results.append({
                    'id': movie['id'],
                    'title': movie['title'],
                    'overview': movie.get('overview', ''),
                    'poster_path': movie.get('poster_path'),
                    'backdrop_path': movie.get('backdrop_path'),
                    'release_date': movie.get('release_date', ''),
                    'vote_average': movie.get('vote_average', 0),
                    'vote_count': movie.get('vote_count', 0),
                    'popularity': movie.get('popularity', 0),
                    'relevance_order': idx,  # Track original TMDB relevance order
                    'media_type': 'movie',
                    'genre_ids': movie.get('genre_ids', [])
                })

        # Search TV shows if requested
        if media_type in ['all', 'tv']:
            tv_url = f"https://api.themoviedb.org/3/search/tv?api_key={tmdb_api_key}&query={query}&page={page}&language=en-US"
            tv_response = requests.get(tv_url, timeout=10)
            tv_response.raise_for_status()
            tv_data = tv_response.json()

            for idx, tv in enumerate(tv_data.get('results', [])):
                results.append({
                    'id': tv['id'],
                    'name': tv['name'],
                    'title': tv['name'],  # Normalize for frontend
                    'overview': tv.get('overview', ''),
                    'poster_path': tv.get('poster_path'),
                    'backdrop_path': tv.get('backdrop_path'),
                    'first_air_date': tv.get('first_air_date', ''),
                    'release_date': tv.get('first_air_date', ''),  # Normalize
                    'vote_average': tv.get('vote_average', 0),
                    'vote_count': tv.get('vote_count', 0),
                    'popularity': tv.get('popularity', 0),
                    'relevance_order': idx,  # Track original TMDB relevance order
                    'media_type': 'tv',
                    'genre_ids': tv.get('genre_ids', [])
                })

        # Enrich with digital release dates
        results = enrich_with_digital_dates(results, media_type, tmdb_api_key)

        # Sort results based on user preference
        # Parse sort_by format: "field.order" (e.g., "popularity.desc", "vote_average.asc")
        sort_field = 'relevance'
        sort_order = 'desc'
        if '.' in sort_by:
            parts = sort_by.split('.')
            sort_field = parts[0]
            sort_order = parts[1] if len(parts) > 1 else 'desc'
        else:
            sort_field = sort_by

        reverse_sort = (sort_order == 'desc')

        if sort_field == 'popularity':
            results.sort(key=lambda x: x.get('popularity', 0), reverse=reverse_sort)
        elif sort_field in ['vote_average', 'rating']:
            results.sort(key=lambda x: x.get('vote_average', 0), reverse=reverse_sort)
        elif sort_field == 'release_date':
            results.sort(key=lambda x: x.get('release_date', '') or x.get('first_air_date', ''), reverse=reverse_sort)
        elif sort_field == 'vote_count':
            results.sort(key=lambda x: x.get('vote_count', 0), reverse=reverse_sort)
        else:  # relevance (default) - keep TMDB's relevance order
            results.sort(key=lambda x: x.get('relevance_order', 999))

        # Add database status and episode info
        add_db_status_and_episode_info(results)

        return jsonify({
            'results': results,
            'page': page,
            'total_pages': max(movie_data.get('total_pages', 1), tv_data.get('total_pages', 1)) if media_type == 'all' else (movie_data.get('total_pages', 1) if media_type == 'movie' else tv_data.get('total_pages', 1)),
            'total_results': len(results)
        })

    except requests.exceptions.RequestException as e:
        logging.error(f"TMDB API error: {e}")
        return jsonify({'error': 'Failed to fetch from TMDB API'}), 500
    except Exception as e:
        logging.error(f"Discover search error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

def _tmdb_api_key_or_error():
    tmdb_api_key = get_setting('TMDB', 'api_key', '')
    if not tmdb_api_key:
        return None, (jsonify({'error': 'TMDB API key not configured'}), 400)
    return tmdb_api_key, None

@discover_bp.route('/api/people')
@user_required
def search_people():
    """Search actors and directors on TMDB."""
    from utilities.tmdb_people import person_search_url, summarize_person
    try:
        query = request.args.get('query', '').strip()
        if not query:
            return jsonify({'results': []})
        tmdb_api_key, err = _tmdb_api_key_or_error()
        if err:
            return err
        data = get_tmdb_json(person_search_url(tmdb_api_key, query), timeout=10)
        people = [summarize_person(p) for p in data.get('results') or [] if p.get('id')]
        return jsonify({'results': people[:8], 'total_results': data.get('total_results', len(people))})
    except requests.exceptions.RequestException as e:
        logging.error(f"TMDB person search error: {e}")
        return jsonify({'error': 'Failed to fetch from TMDB API'}), 500
    except Exception as e:
        logging.error(f"Discover person search error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@discover_bp.route('/api/person/<int:person_id>')
@user_required
def person_filmography(person_id):
    """Return a person's movie and TV credits, with library status."""
    from utilities.tmdb_people import person_details_url, person_payload
    try:
        role = request.args.get('role', 'all')
        media_type = request.args.get('type', 'all')
        tmdb_api_key, err = _tmdb_api_key_or_error()
        if err:
            return err
        details = get_tmdb_json(person_details_url(tmdb_api_key, person_id), timeout=15)
        if not details.get('id'):
            return jsonify({'error': 'Person not found'}), 404
        payload = person_payload(details, role=role)
        results = payload['results']
        if media_type in ('movie', 'tv'):
            results = [r for r in results if r.get('media_type') == media_type]
        add_db_status_and_episode_info(results, use_battery=False)
        payload['results'] = results
        payload['total_results'] = len(results)
        return jsonify(payload)
    except requests.exceptions.HTTPError as e:
        status = getattr(e.response, 'status_code', 500)
        if status == 404:
            return jsonify({'error': 'Person not found'}), 404
        logging.error(f"TMDB person details error: {e}")
        return jsonify({'error': 'Failed to fetch from TMDB API'}), 500
    except requests.exceptions.RequestException as e:
        logging.error(f"TMDB person details error: {e}")
        return jsonify({'error': 'Failed to fetch from TMDB API'}), 500
    except Exception as e:
        logging.error(f"Discover person details error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

def _request_filmography_item(item, versions, source_detail):
    """Add one movie or show from a person filmography. Returns a result dict."""
    from metadata.metadata import process_metadata
    from database.wanted_items import add_wanted_items
    from cli_battery.app.direct_api import DirectAPI

    tmdb_id = str(item.get('id') or '')
    media_type = (item.get('media_type') or item.get('mediaType') or '').lower()
    title = item.get('title') or item.get('name') or 'Unknown'
    if media_type == 'show':
        media_type = 'tv'
    if not tmdb_id or media_type not in ('movie', 'tv'):
        return {'id': tmdb_id, 'title': title, 'ok': False, 'error': 'Invalid item'}

    lookup_type = 'movie' if media_type == 'movie' else 'show'
    try:
        imdb_id, _ = DirectAPI.tmdb_to_imdb(tmdb_id, media_type=lookup_type)
    except Exception as e:
        return {'id': tmdb_id, 'title': title, 'ok': False, 'error': str(e)}
    if not imdb_id:
        return {'id': tmdb_id, 'title': title, 'ok': False, 'error': 'No IMDb ID'}

    wanted_item = {
        'imdb_id': imdb_id,
        'tmdb_id': tmdb_id,
        'media_type': media_type,
        'title': title,
        'year': (item.get('release_date') or item.get('first_air_date') or item.get('year') or '')[:4],
        'versions': versions,
    }
    if media_type == 'tv':
        try:
            seasons_data, _ = DirectAPI.get_show_seasons(imdb_id)
            if seasons_data:
                season_numbers = [
                    int(n) for n in seasons_data.keys()
                    if str(n).isdigit() and int(n) > 0
                ]
                if season_numbers:
                    wanted_item['requested_seasons'] = season_numbers
        except Exception as e:
            logging.debug(f"Person request: seasons for {imdb_id}: {e}")

    processed = process_metadata([wanted_item]) or {}
    all_items = processed.get('movies', []) + processed.get('episodes', [])
    if not all_items:
        return {'id': tmdb_id, 'title': title, 'ok': False, 'error': 'No metadata'}
    for row in all_items:
        row['content_source'] = 'content_requester'
        row['content_source_detail'] = source_detail
    added = add_wanted_items(all_items, versions)
    return {'id': tmdb_id, 'title': title, 'ok': True, 'items_added': added}

@discover_bp.route('/api/request-batch', methods=['POST'])
@user_required
def request_batch():
    """Request up to 15 titles at once (person filmography Request all)."""
    from routes.utils import is_user_system_enabled
    try:
        data = request.get_json(silent=True) or {}
        items = data.get('items') or []
        selected_versions = data.get('versions') or []
        if not items:
            return jsonify({'error': 'No titles to request'}), 400
        if not selected_versions:
            return jsonify({'error': 'Select at least one version'}), 400
        versions = {version: True for version in selected_versions}
        if is_user_system_enabled() and current_user.is_authenticated:
            source_detail = current_user.username
        else:
            source_detail = 'CD-Discover'
        results = []
        for item in items[:15]:
            results.append(_request_filmography_item(item, versions, source_detail))
        requested = sum(1 for r in results if r.get('ok') and r.get('items_added'))
        already = sum(1 for r in results if r.get('ok') and not r.get('items_added'))
        failed = [r for r in results if not r.get('ok')]
        return jsonify({
            'success': True,
            'requested': requested,
            'already': already,
            'failed': failed,
            'results': results,
        })
    except Exception as e:
        logging.error(f"Discover request-batch error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@discover_bp.route('/api/person/<int:person_id>/plex-collection', methods=['POST'])
@user_required
def person_plex_collection(person_id):
    """Create Plex collections from this person's titles that are already in Plex."""
    from utilities.tmdb_people import person_details_url, person_payload
    try:
        data = request.get_json(silent=True) or {}
        role = data.get('role') or 'all'
        media_type = data.get('type') or 'all'
        tmdb_api_key, err = _tmdb_api_key_or_error()
        if err:
            return err
        details = get_tmdb_json(person_details_url(tmdb_api_key, person_id), timeout=15)
        if not details.get('id'):
            return jsonify({'error': 'Person not found'}), 404
        payload = person_payload(details, role=role)
        works = payload['results']
        if media_type in ('movie', 'tv'):
            works = [w for w in works if w.get('media_type') == media_type]
        from database.plex_collections import create_named_collections_from_tmdb, remember_person_collection
        name = payload['person']['name'] or f'Person {person_id}'
        remember_person_collection(person_id, name, role, media_type, works)
        result = create_named_collections_from_tmdb(name, works)
        return jsonify({'success': True, 'person': name, **result})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logging.error(f"Discover person Plex collection error: {e}")
        return jsonify({'error': 'Could not create the Plex collection'}), 500
