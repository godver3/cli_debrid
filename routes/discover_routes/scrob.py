"""Scrob lists."""

import logging
import requests
from datetime import datetime
from flask import jsonify, request
from utilities.settings import get_setting
from routes.models import user_required
from ._common import _SCROB_LISTS_INDEX_TTL, _SCROB_LIST_ITEMS_TTL, _SCROB_SPECIAL_TTL, _scrob_cache, _scrob_cache_lock, discover_bp
from .enrich import add_db_status_and_episode_info

def _scrob_items_to_discover(raw_items, media_type_filter, tmdb_api_key):
    """Shared helper: take raw Scrob media items, enrich with TMDB, return
    discover-shaped results. Mirrors _trakt_items_to_discover — Discover display
    only needs tmdb_id (unlike the Wanted-queue path, which needs imdb_id via
    process_scrob_items), so this intentionally skips that conversion.
    """
    from concurrent.futures import ThreadPoolExecutor

    candidates = []
    for item in raw_items:
        # Scrob's list-item shape wraps the media dict under 'media'; discover/
        # special-list endpoints return the media dict directly (same as Trakt).
        media = item.get('media', item)
        raw_type = (media.get('type') or '').lower()
        if raw_type == 'movie':
            mt = 'movie'
            tmdb_id = media.get('tmdb_id')
        elif raw_type == 'episode':
            # Episode tmdb_id is a separate ID namespace from shows — use the
            # parent show's tmdb_id, same reasoning as process_scrob_items.
            mt = 'tv'
            tmdb_id = media.get('show_tmdb_id')
        elif raw_type in ('series', 'show', 'tv'):
            mt = 'tv'
            tmdb_id = media.get('tmdb_id')
        else:
            continue

        if not tmdb_id:
            continue
        if media_type_filter not in ('all', '') and mt != media_type_filter:
            continue
        candidates.append({'tmdb_id': tmdb_id, 'media_type': mt})

    if not tmdb_api_key or not candidates:
        return []

    def fetch_one(c):
        tmdb_id, mt = c['tmdb_id'], c['media_type']
        endpoint = 'tv' if mt == 'tv' else 'movie'
        try:
            r = requests.get(
                f'https://api.themoviedb.org/3/{endpoint}/{tmdb_id}?api_key={tmdb_api_key}&language=en-US',
                timeout=8
            )
            if not r.ok:
                return None
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
                'media_type': mt,
                'genre_ids': [g['id'] for g in d.get('genres', [])],
                'original_language': d.get('original_language', ''),
            }
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=10) as ex:
        enriched = list(ex.map(fetch_one, candidates))

    results = [r for r in enriched if r is not None]
    add_db_status_and_episode_info(results, use_battery=False)
    return results

@discover_bp.route('/api/scrob/lists')
@user_required
def scrob_personal_lists():
    """Return the user's Scrob custom lists, cached for 10 minutes."""
    try:
        with _scrob_cache_lock:
            entry = _scrob_cache.get('lists-index')
            if entry and datetime.now() < entry['expires']:
                return jsonify({'success': True, 'lists': entry['data'], 'cached': True})

        from content_checkers.scrob import get_scrob_config, _scrob_get
        if not get_scrob_config():
            return jsonify({'success': False, 'lists': [], 'error': 'Scrob not configured'})

        data = _scrob_get('/lists')
        if data is None:
            return jsonify({'success': False, 'lists': [], 'error': 'Failed to fetch Scrob lists'})

        lists = [
            {'id': lst['id'], 'name': lst.get('name', f"List {lst['id']}"), 'item_count': lst.get('item_count', 0)}
            for lst in data.get('lists', [])
        ]
        with _scrob_cache_lock:
            _scrob_cache['lists-index'] = {'data': lists, 'expires': datetime.now() + _SCROB_LISTS_INDEX_TTL}
        return jsonify({'success': True, 'lists': lists})
    except Exception as e:
        logging.error(f"[Scrob Personal] Failed to fetch lists: {e}")
        with _scrob_cache_lock:
            entry = _scrob_cache.get('lists-index')
            if entry:
                return jsonify({'success': True, 'lists': entry['data'], 'cached': True, 'stale': True})
        return jsonify({'success': False, 'lists': [], 'error': str(e)}), 500

@discover_bp.route('/api/scrob/list/<list_id>')
@user_required
def scrob_list_items(list_id):
    """Fetch items from a Scrob custom list by ID, enriched with TMDB data."""
    try:
        media_type_filter = request.args.get('type', 'all')
        cache_key = f'list:{list_id}:{media_type_filter}'

        with _scrob_cache_lock:
            entry = _scrob_cache.get(cache_key)
            if entry and datetime.now() < entry['expires']:
                return jsonify({'success': True, 'results': entry['data'], 'total_results': len(entry['data']), 'cached': True})

        from content_checkers.scrob import get_scrob_config, _scrob_get
        if not get_scrob_config():
            return jsonify({'success': False, 'results': [], 'error': 'Scrob not configured'})

        data = _scrob_get(f'/lists/{list_id}')
        if data is None:
            return jsonify({'success': False, 'results': [], 'error': 'Failed to fetch Scrob list'})

        raw_items = data.get('items', [])
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        results = _scrob_items_to_discover(raw_items, media_type_filter, tmdb_api_key)

        if results:  # Don't cache empty results — likely upstream error or genuinely-empty list
            with _scrob_cache_lock:
                _scrob_cache[cache_key] = {'data': results, 'expires': datetime.now() + _SCROB_LIST_ITEMS_TTL}

        return jsonify({'success': True, 'results': results, 'total_results': len(results)})
    except Exception as e:
        logging.error(f"[Scrob Personal] Error fetching list {list_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'results': [], 'error': str(e)}), 500

@discover_bp.route('/api/scrob/special/<list_type>')
@user_required
def scrob_special_list(list_type):
    """Fetch a Scrob special list (Trending, Popular, etc.) and return discover-shaped results."""
    try:
        from content_checkers.scrob import get_scrob_config, _scrob_get, SPECIAL_LIST_ENDPOINTS

        if list_type not in SPECIAL_LIST_ENDPOINTS:
            return jsonify({'success': False, 'results': [], 'error': f"Unknown special list type '{list_type}'"}), 404

        media_type_filter = request.args.get('type', 'all')  # all, movie, tv
        cache_key = f'special:{list_type}:{media_type_filter}'

        with _scrob_cache_lock:
            entry = _scrob_cache.get(cache_key)
            if entry and datetime.now() < entry['expires']:
                return jsonify({'success': True, 'results': entry['data'], 'total_results': len(entry['data']), 'cached': True})

        if not get_scrob_config():
            return jsonify({'success': False, 'results': [], 'error': 'Scrob not configured'})

        api_paths = SPECIAL_LIST_ENDPOINTS[list_type]
        endpoints_to_call = []
        if media_type_filter in ('all', 'movie') and api_paths.get('movies'):
            endpoints_to_call.append(api_paths['movies'])
        if media_type_filter in ('all', 'tv') and api_paths.get('shows'):
            # Scrob's For You endpoint returns both media types. Avoid calling
            # the same endpoint twice when Discover requests the combined feed.
            if api_paths['shows'] not in endpoints_to_call:
                endpoints_to_call.append(api_paths['shows'])

        raw_items = []
        for endpoint_path, endpoint_params in endpoints_to_call:
            data = _scrob_get(endpoint_path, params=endpoint_params)
            if data:
                raw_items.extend(data.get('results', []))

        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        results = _scrob_items_to_discover(raw_items, media_type_filter, tmdb_api_key)

        if results:
            with _scrob_cache_lock:
                _scrob_cache[cache_key] = {'data': results, 'expires': datetime.now() + _SCROB_SPECIAL_TTL}

        return jsonify({'success': True, 'results': results, 'total_results': len(results)})
    except Exception as e:
        logging.error(f"[Scrob Special] Error fetching '{list_type}': {e}", exc_info=True)
        return jsonify({'success': False, 'results': [], 'error': str(e)}), 500
