"""MDBList lists and personal lists."""

import logging
import requests
from datetime import datetime
from flask import jsonify, request
from utilities.tmdb_cache import get_cached_db_statuses
from utilities.settings import get_setting
from utilities.mdblist_api import is_mdblist_configured, fetch_mdblist_top_lists, fetch_list_items, get_user_lists as get_mdblist_user_lists, fetch_custom_list_items, test_api_connection as test_mdblist_connection
from routes.models import user_required
from ._common import _MDBLIST_PERSONAL_INDEX_TTL, _MDBLIST_PERSONAL_ITEMS_TTL, _mdblist_personal_cache, _mdblist_personal_cache_lock, _save_mdblist_personal_file_cache, discover_bp

# =============================================================================
# MDBList Integration API
# =============================================================================

@discover_bp.route('/api/mdblist/status')
@user_required
def mdblist_status():
    """
    Check if MDBList is configured and test connection
    """
    try:
        if not is_mdblist_configured():
            return jsonify({
                'configured': False,
                'message': 'MDBList API key not configured'
            })

        # Test the connection
        result = test_mdblist_connection()
        return jsonify({
            'configured': True,
            'connected': result.get('success', False),
            'message': result.get('message') or result.get('error', '')
        })

    except Exception as e:
        logging.error(f"MDBList status check error: {e}")
        return jsonify({
            'configured': False,
            'error': str(e)
        })

@discover_bp.route('/api/mdblist/lists')
@user_required
def mdblist_available_lists():
    """
    Get available MDBList curated lists.
    Note: Public curated lists don't require an MDBList API key.
    """
    try:
        result = fetch_mdblist_top_lists()
        return jsonify(result)

    except Exception as e:
        logging.error(f"Error fetching MDBList available lists: {e}")
        return jsonify({'error': str(e), 'lists': []}), 500

@discover_bp.route('/api/mdblist/list/<list_key>')
@user_required
def mdblist_list_items(list_key):
    """
    Get items from a specific MDBList curated list.
    Returns items with TMDB IDs that can be used to fetch full metadata.
    Note: Public curated lists don't require an MDBList API key.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    try:
        limit = request.args.get('limit', 20, type=int)
        media_type_filter = request.args.get('type', 'all')  # all, movie, tv

        # Fetch list items from MDBList
        result = fetch_list_items(list_key, limit=min(limit, 50))

        if 'error' in result:
            return jsonify(result), 400

        items = result.get('items', [])

        # Filter by media type if requested
        if media_type_filter != 'all':
            items = [item for item in items if item.get('media_type') == media_type_filter]

        # Enrich items with TMDB data and database status
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        enriched_items = []

        if tmdb_api_key and items:
            # Get database status for all items upfront
            tmdb_ids = [str(item['tmdb_id']) for item in items if item.get('tmdb_id')]
            db_statuses = get_cached_db_statuses(tmdb_ids) if tmdb_ids else {}

            def enrich_item(item, idx):
                """Helper function to enrich a single item with TMDB data"""
                tmdb_id = item.get('tmdb_id')
                if not tmdb_id:
                    return None

                media_type = item.get('media_type', 'movie')
                rank = item.get('rank', idx + 1)

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
                            'db_status': db_statuses.get(str(tmdb_id), 'missing'),
                            'rank': rank,
                            '_idx': idx  # Keep for ordering
                        }
                    else:
                        return {
                            'id': tmdb_id,
                            'title': item.get('title', 'Unknown'),
                            'media_type': media_type,
                            'db_status': db_statuses.get(str(tmdb_id), 'missing'),
                            'rank': rank,
                            '_idx': idx
                        }
                except Exception as e:
                    logging.debug(f"TMDB lookup failed for {tmdb_id}: {e}")
                    return {
                        'id': tmdb_id,
                        'title': item.get('title', 'Unknown'),
                        'media_type': media_type,
                        'db_status': db_statuses.get(str(tmdb_id), 'missing'),
                        'rank': rank,
                        '_idx': idx
                    }

            # Use ThreadPoolExecutor for parallel TMDB requests
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = {executor.submit(enrich_item, item, idx): idx for idx, item in enumerate(items)}
                results_map = {}

                for future in as_completed(futures):
                    idx = futures[future]
                    try:
                        enriched = future.result()
                        if enriched:
                            results_map[idx] = enriched
                    except Exception as e:
                        logging.warning(f"Error enriching MDBList item: {e}")

                # Sort by original index to maintain order
                enriched_items = [results_map[k] for k in sorted(results_map.keys())]
                # Clean up internal index field
                for item in enriched_items:
                    item.pop('_idx', None)

        return jsonify({
            'success': True,
            'list_name': result.get('list_name', list_key),
            'results': enriched_items,
            'total_results': len(enriched_items)
        })

    except Exception as e:
        logging.error(f"Error fetching MDBList list {list_key}: {e}")
        return jsonify({'error': str(e)}), 500

# =============================================================================
# MDBList Personal Lists API (Requires API Key)
# =============================================================================

@discover_bp.route('/api/mdblist/personal-lists')
@user_required
def mdblist_personal_lists():
    """Return the user's personal MDBList lists, cached for 10 minutes."""
    try:
        with _mdblist_personal_cache_lock:
            entry = _mdblist_personal_cache.get('index')
            if entry and datetime.now() < entry['expires']:
                return jsonify({'success': True, 'lists': entry['data'], 'cached': True})

        if not is_mdblist_configured():
            return jsonify({'success': False, 'lists': [], 'error': 'MDBList API key not configured'})

        result = get_mdblist_user_lists()
        if 'error' in result and not result.get('lists'):
            return jsonify({'success': False, 'lists': [], 'error': result['error']})

        # MDBList returns a separate entry per mediatype for mixed lists — deduplicate by slug,
        # keeping the entry with the highest item count (which covers both types).
        seen_slugs = {}
        for lst in result.get('lists', []):
            slug = lst.get('slug', '')
            if slug not in seen_slugs or lst.get('items_count', 0) > seen_slugs[slug].get('items_count', 0):
                seen_slugs[slug] = lst
        lists = list(seen_slugs.values())
        with _mdblist_personal_cache_lock:
            _mdblist_personal_cache['index'] = {
                'data': lists,
                'expires': datetime.now() + _MDBLIST_PERSONAL_INDEX_TTL,
            }
        return jsonify({'success': True, 'lists': lists})
    except Exception as e:
        logging.error(f"[MDBList Personal] Failed to fetch personal lists: {e}")
        with _mdblist_personal_cache_lock:
            entry = _mdblist_personal_cache.get('index')
            if entry:
                return jsonify({'success': True, 'lists': entry['data'], 'cached': True, 'stale': True})
        return jsonify({'success': False, 'lists': [], 'error': str(e)}), 500

@discover_bp.route('/api/mdblist/personal-list/<list_id>')
@user_required
def mdblist_personal_list_items(list_id):
    """Fetch items from a user's personal MDBList list by ID, enriched with TMDB data."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    try:
        cache_key = str(list_id)

        with _mdblist_personal_cache_lock:
            entry = _mdblist_personal_cache.get(cache_key)
            if entry and datetime.now() < entry['expires'] and entry['data']:
                return jsonify({'success': True, 'results': entry['data'], 'total_results': len(entry['data']), 'cached': True})
            elif entry and not entry['data']:
                # Evict empty cache entry so we re-fetch with correct endpoint
                del _mdblist_personal_cache[cache_key]

        # Look up slug + username from the index cache
        slug = None
        username = None
        with _mdblist_personal_cache_lock:
            index_entry = _mdblist_personal_cache.get('index')
            if index_entry:
                for lst in index_entry.get('data', []):
                    if str(lst.get('id')) == str(list_id):
                        slug = lst.get('slug')
                        username = lst.get('username')
                        break

        logging.info(f"[MDBList Personal] Fetching list_id={list_id} slug={slug} username={username}")
        result = fetch_custom_list_items(list_id, limit=100, username=username, slug=slug)
        logging.info(f"[MDBList Personal] fetch_custom_list_items returned: success={result.get('success')}, items={len(result.get('items', []))}, error={result.get('error')}")
        if 'error' in result and not result.get('items'):
            return jsonify({'success': False, 'results': [], 'error': result.get('error', 'Unknown error')}), 400

        items = result.get('items', [])

        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        enriched_items = []

        if tmdb_api_key and items:
            tmdb_ids = [str(i['tmdb_id']) for i in items if i.get('tmdb_id')]
            db_statuses = get_cached_db_statuses(tmdb_ids) if tmdb_ids else {}

            def enrich_item(item, idx):
                tmdb_id = item.get('tmdb_id')
                if not tmdb_id:
                    return None
                media_type = item.get('media_type', 'movie')
                endpoint = 'tv' if media_type == 'tv' else 'movie'
                try:
                    r = requests.get(
                        f'https://api.themoviedb.org/3/{endpoint}/{tmdb_id}?api_key={tmdb_api_key}&language=en-US',
                        timeout=8
                    )
                    if r.ok:
                        d = r.json()
                        return {
                            'id': tmdb_id,
                            'title': d.get('title') or d.get('name', ''),
                            'name': d.get('name') or d.get('title', ''),
                            'overview': d.get('overview', ''),
                            'poster_path': d.get('poster_path'),
                            'backdrop_path': d.get('backdrop_path'),
                            'release_date': d.get('release_date', ''),
                            'first_air_date': d.get('first_air_date', ''),
                            'vote_average': d.get('vote_average', 0),
                            'vote_count': d.get('vote_count', 0),
                            'media_type': media_type,
                            'genre_ids': [g['id'] for g in d.get('genres', [])],
                            'original_language': d.get('original_language', ''),
                            'db_status': db_statuses.get(str(tmdb_id), 'missing'),
                            'rank': item.get('rank', idx + 1),
                            '_idx': idx,
                        }
                except Exception:
                    pass
                return {
                    'id': tmdb_id,
                    'title': item.get('title', 'Unknown'),
                    'media_type': media_type,
                    'db_status': db_statuses.get(str(tmdb_id), 'missing'),
                    'rank': item.get('rank', idx + 1),
                    '_idx': idx,
                }

            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = {executor.submit(enrich_item, item, idx): idx for idx, item in enumerate(items)}
                results_map = {}
                for future in as_completed(futures):
                    idx = futures[future]
                    try:
                        enriched = future.result()
                        if enriched:
                            results_map[idx] = enriched
                    except Exception as e:
                        logging.warning(f"Error enriching MDBList personal list item: {e}")

            enriched_items = [results_map[k] for k in sorted(results_map.keys())]
            for item in enriched_items:
                item.pop('_idx', None)

        if enriched_items:  # Don't cache empty results — likely upstream API error
            with _mdblist_personal_cache_lock:
                _mdblist_personal_cache[cache_key] = {
                    'data': enriched_items,
                    'expires': datetime.now() + _MDBLIST_PERSONAL_ITEMS_TTL,
                }
            _save_mdblist_personal_file_cache()

        logging.info(f"[MDBList Personal] Returning {len(enriched_items)} enriched items for list_id={list_id}")
        return jsonify({'success': True, 'results': enriched_items, 'total_results': len(enriched_items)})

    except Exception as e:
        logging.error(f"[MDBList Personal] Error fetching list {list_id}: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
