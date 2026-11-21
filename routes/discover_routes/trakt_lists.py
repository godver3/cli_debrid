"""Trakt personal, special and my lists."""

import hashlib
import logging
import requests
from datetime import datetime
from flask import jsonify, request
from utilities.settings import get_setting
from routes.models import user_required
from ._common import _TRAKT_LISTS_INDEX_TTL, _TRAKT_MYLIST_TTL, _TRAKT_SPECIAL_TTL, _save_mylist_file_cache, _trakt_cache_lock, _trakt_lists_cache, discover_bp
from .enrich import add_db_status_and_episode_info

# =============================================================================
# FlixPatrol Integration API (No API Key Required)
# =============================================================================

@discover_bp.route('/api/trakt/lists')
@user_required
def trakt_personal_lists():
    """Return the user's personal Trakt lists, cached for 10 minutes."""
    try:
        with _trakt_cache_lock:
            entry = _trakt_lists_cache.get('index')
            if entry and datetime.now() < entry['expires']:
                return jsonify({'success': True, 'lists': entry['data'], 'cached': True})

        from content_checkers.trakt import get_trakt_config
        trakt_config = get_trakt_config()
        access_token = trakt_config.get('OAUTH_TOKEN', '')
        client_id = trakt_config.get('CLIENT_ID', '') or get_setting('Trakt', 'client_id', '')
        if not access_token or not client_id:
            return jsonify({'success': False, 'lists': [], 'error': 'Trakt not authenticated'})
        headers = {
            'Content-Type': 'application/json',
            'trakt-api-version': '2',
            'trakt-api-key': client_id,
            'Authorization': f'Bearer {access_token}',
        }
        resp = requests.get('https://api.trakt.tv/users/me/lists', headers=headers, timeout=15)
        resp.raise_for_status()
        lists = []
        for item in resp.json():
            lists.append({
                'name': item.get('name', ''),
                'slug': item.get('ids', {}).get('slug', ''),
                'item_count': item.get('item_count', 0),
                'description': item.get('description', ''),
                'updated_at': item.get('updated_at', ''),
            })
        with _trakt_cache_lock:
            _trakt_lists_cache['index'] = {
                'data': lists,
                'expires': datetime.now() + _TRAKT_LISTS_INDEX_TTL,
            }
        return jsonify({'success': True, 'lists': lists})
    except Exception as e:
        logging.error(f"[Personal] Failed to fetch Trakt personal lists: {e}")
        # Return stale cache if available rather than an error
        with _trakt_cache_lock:
            entry = _trakt_lists_cache.get('index')
            if entry:
                return jsonify({'success': True, 'lists': entry['data'], 'cached': True, 'stale': True})
        return jsonify({'success': False, 'lists': [], 'error': str(e)}), 500

def _trakt_items_to_discover(items, media_type_filter, tmdb_api_key):
    """Shared helper: take raw Trakt list items, enrich with TMDB, return discover-shaped results."""
    from concurrent.futures import ThreadPoolExecutor

    # Normalise — Trakt list items wrap the media object under 'movie'/'show'
    candidates = []
    for item in items:
        mt = item.get('type', '')  # 'movie' or 'show' or 'episode'
        if mt == 'episode':
            mt = 'show'
        if media_type_filter not in ('all', '') and mt != media_type_filter:
            continue
        media_obj = item.get('movie') or item.get('show') or item  # top-level for special lists
        ids = media_obj.get('ids', {})
        tmdb_id = ids.get('tmdb')
        if not tmdb_id:
            continue
        candidates.append({'tmdb_id': tmdb_id, 'media_type': 'tv' if mt in ('show', 'tv') else 'movie'})

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

@discover_bp.route('/api/trakt/special/<list_type>')
@user_required
def trakt_special_list(list_type):
    """Fetch a Trakt special list (trending, popular, etc.) and return discover-shaped results.

    Caching strategy: content-hash fingerprint + 1-hour hard TTL.
    Special lists have no updated_at timestamp, so we hash the ordered list of
    TMDB IDs to detect real changes. If the content hash is unchanged we extend
    the TTL instead of re-fetching. On any fetch failure we return stale cache.
    """
    try:
        from content_checkers.trakt import get_trakt_config
        trakt_config = get_trakt_config()
        access_token = trakt_config.get('OAUTH_TOKEN', '')
        client_id = trakt_config.get('CLIENT_ID', '') or get_setting('Trakt', 'client_id', '')
        if not access_token or not client_id:
            return jsonify({'success': False, 'results': [], 'error': 'Trakt not authenticated'})

        headers = {
            'Content-Type': 'application/json',
            'trakt-api-version': '2',
            'trakt-api-key': client_id,
            'Authorization': f'Bearer {access_token}',
        }
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        media_type_filter = request.args.get('type', 'all')  # all, movie, tv

        SPECIAL_ENDPOINTS = {
            'trending':    {'movie': '/movies/trending',          'show': '/shows/trending'},
            'popular':     {'movie': '/movies/popular',           'show': '/shows/popular'},
            'favorited':   {'movie': '/movies/favorited/weekly',  'show': '/shows/favorited/weekly'},
            'played':      {'movie': '/movies/played/weekly',     'show': '/shows/played/weekly'},
            'watched':     {'movie': '/movies/watched/weekly',    'show': '/shows/watched/weekly'},
            'collected':   {'movie': '/movies/collected/weekly',  'show': '/shows/collected/weekly'},
            'anticipated': {'movie': '/movies/anticipated',       'show': '/shows/anticipated'},
            'boxoffice':   {'movie': '/movies/boxoffice',         'show': None},
            'recommendations': {'movie': '/recommendations/movies', 'show': '/recommendations/shows'},
        }

        if list_type not in SPECIAL_ENDPOINTS:
            return jsonify({'success': False, 'results': [], 'error': f'Unknown list type: {list_type}'}), 400

        cache_key = f'special:{list_type}:{media_type_filter}'

        # Check hard TTL — serve cache if still fresh
        with _trakt_cache_lock:
            entry = _trakt_lists_cache.get(cache_key)
        if entry and datetime.now() < entry.get('expires', datetime.min):
            logging.debug(f"[Personal] Special list '{list_type}' serving from cache (TTL fresh)")
            return jsonify({'success': True, 'results': entry['data'], 'total_results': len(entry['data']), 'cached': True})

        endpoints = SPECIAL_ENDPOINTS[list_type]
        fetch_types = []
        if media_type_filter in ('all', 'movie') and endpoints.get('movie'):
            fetch_types.append(('movie', endpoints['movie']))
        if media_type_filter in ('all', 'tv') and endpoints.get('show'):
            fetch_types.append(('tv', endpoints['show']))

        raw_items = []
        fetch_ok = True
        for mt, ep in fetch_types:
            try:
                r = requests.get(
                    f'https://api.trakt.tv{ep}?limit=40&extended=full',
                    headers=headers, timeout=15
                )
                r.raise_for_status()
                for item in r.json():
                    media_obj = item.get('movie') or item.get('show') or item
                    raw_items.append({'type': mt, 'movie' if mt == 'movie' else 'show': media_obj})
            except Exception as e:
                logging.warning(f"[Personal] Special list {list_type}/{mt} fetch error: {e}")
                fetch_ok = False

        # On fetch failure, return stale cache if available
        if not fetch_ok and not raw_items and entry:
            logging.warning(f"[Personal] Special list '{list_type}' fetch failed, returning stale cache")
            return jsonify({'success': True, 'results': entry['data'], 'total_results': len(entry['data']), 'stale': True})

        # Compute content hash from ordered TMDB IDs to detect real list changes
        tmdb_ids = []
        for item in raw_items:
            media_obj = item.get('movie') or item.get('show') or {}
            tid = (media_obj.get('ids') or {}).get('tmdb')
            if tid:
                tmdb_ids.append(str(tid))
        content_hash = hashlib.md5(','.join(tmdb_ids).encode()).hexdigest()

        # If content unchanged since last fetch, just extend TTL without re-enriching
        if entry and entry.get('content_hash') == content_hash:
            logging.debug(f"[Personal] Special list '{list_type}' content unchanged, extending TTL")
            with _trakt_cache_lock:
                _trakt_lists_cache[cache_key]['expires'] = datetime.now() + _TRAKT_SPECIAL_TTL
            return jsonify({'success': True, 'results': entry['data'], 'total_results': len(entry['data']), 'cached': True})

        results = _trakt_items_to_discover(raw_items, media_type_filter, tmdb_api_key)

        with _trakt_cache_lock:
            _trakt_lists_cache[cache_key] = {
                'data': results,
                'content_hash': content_hash,
                'expires': datetime.now() + _TRAKT_SPECIAL_TTL,
            }
        logging.info(f"[Personal] Special list '{list_type}' cached ({len(results)} results, hash={content_hash[:8]})")
        return jsonify({'success': True, 'results': results, 'total_results': len(results)})

    except Exception as e:
        logging.error(f"[Personal] Special list error ({list_type}): {e}", exc_info=True)
        return jsonify({'success': False, 'results': [], 'error': str(e)}), 500

@discover_bp.route('/api/trakt/mylist/<slug>')
@user_required
def trakt_mylist(slug):
    """Fetch items from a user's personal Trakt list by slug, with updated_at-based cache invalidation."""
    try:
        from content_checkers.trakt import get_trakt_config
        trakt_config = get_trakt_config()
        access_token = trakt_config.get('OAUTH_TOKEN', '')
        client_id = trakt_config.get('CLIENT_ID', '') or get_setting('Trakt', 'client_id', '')
        if not access_token or not client_id:
            return jsonify({'success': False, 'results': [], 'error': 'Trakt not authenticated'})

        headers = {
            'Content-Type': 'application/json',
            'trakt-api-version': '2',
            'trakt-api-key': client_id,
            'Authorization': f'Bearer {access_token}',
        }
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        media_type_filter = request.args.get('type', 'all')
        cache_key = f'{slug}:{media_type_filter}'
        remote_updated_at = ''  # resolved lazily below

        # Check cache
        with _trakt_cache_lock:
            entry = _trakt_lists_cache.get(cache_key)

        if entry:
            ttl_fresh = datetime.now() < entry.get('expires', datetime.min)
            if ttl_fresh:
                # TTL still valid — serve immediately without any network call
                logging.debug(f"[Personal] Serving cached mylist '{slug}' (TTL fresh)")
                return jsonify({'success': True, 'results': entry['data'],
                               'total_results': len(entry['data']), 'cached': True})

            # TTL expired — check updated_at before doing full re-fetch
            cached_updated_at = entry.get('updated_at', '')
            remote_updated_at = ''
            try:
                meta_r = requests.get(
                    f'https://api.trakt.tv/users/me/lists/{slug}',
                    headers=headers, timeout=10
                )
                if meta_r.status_code == 200:
                    remote_updated_at = meta_r.json().get('updated_at', '')
            except Exception:
                pass

            if cached_updated_at and remote_updated_at and cached_updated_at == remote_updated_at:
                # Content unchanged — extend TTL and serve cache
                with _trakt_cache_lock:
                    _trakt_lists_cache[cache_key]['expires'] = datetime.now() + _TRAKT_MYLIST_TTL
                _save_mylist_file_cache()
                logging.debug(f"[Personal] Mylist '{slug}' updated_at unchanged, extending TTL")
                return jsonify({'success': True, 'results': entry['data'],
                               'total_results': len(entry['data']), 'cached': True})

        # Full fetch — fetch updated_at if not already retrieved (no prior cache existed)
        if not remote_updated_at:
            try:
                meta_r = requests.get(
                    f'https://api.trakt.tv/users/me/lists/{slug}',
                    headers=headers, timeout=10
                )
                if meta_r.status_code == 200:
                    remote_updated_at = meta_r.json().get('updated_at', '')
            except Exception:
                pass

        raw_items = []
        page = 1
        while True:
            r = requests.get(
                f'https://api.trakt.tv/users/me/lists/{slug}/items?extended=full&limit=100&page={page}',
                headers=headers, timeout=15
            )
            r.raise_for_status()
            page_items = r.json()
            if not page_items:
                break
            raw_items.extend(page_items)
            total_pages = int(r.headers.get('X-Pagination-Page-Count', 1))
            if page >= total_pages:
                break
            page += 1

        results = _trakt_items_to_discover(raw_items, media_type_filter, tmdb_api_key)

        with _trakt_cache_lock:
            _trakt_lists_cache[cache_key] = {
                'data': results,
                'updated_at': remote_updated_at,
                'expires': datetime.now() + _TRAKT_MYLIST_TTL,
            }
        _save_mylist_file_cache()

        return jsonify({'success': True, 'results': results, 'total_results': len(results)})

    except Exception as e:
        logging.error(f"[Personal] My list error ({slug}): {e}", exc_info=True)
        # Return stale cache if available
        cache_key = f'{slug}:{request.args.get("type", "all")}'
        with _trakt_cache_lock:
            entry = _trakt_lists_cache.get(cache_key)
            if entry:
                return jsonify({'success': True, 'results': entry['data'],
                               'total_results': len(entry['data']), 'cached': True, 'stale': True})
        return jsonify({'success': False, 'results': [], 'error': str(e)}), 500
