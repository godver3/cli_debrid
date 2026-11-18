"""Background Discover cache prewarm started at import."""

import hashlib
import logging
import requests
import threading
from datetime import datetime
from utilities.settings import get_setting
from utilities.flixpatrol_api import fetch_top10 as fetch_flixpatrol_top10
from ._common import _FLIXPATROL_TTL, _TRAKT_LISTS_INDEX_TTL, _TRAKT_MYLIST_TTL, _TRAKT_SPECIAL_TTL, _TRENDING_TTL, _flixpatrol_cache, _flixpatrol_cache_lock, _save_mylist_file_cache, _trakt_cache_lock, _trakt_lists_cache, _trending_cache, _trending_cache_lock
from .enrich import add_db_status_and_episode_info, enrich_with_digital_dates
from .trakt_lists import _trakt_items_to_discover

# ── Startup cache prewarming ───────────────────────────────────────────────────

def _prewarm_discover_cache():
    """
    Pre-populate the in-memory caches for trending, FlixPatrol top10, Trakt
    special lists, and Trakt my-list index in a background thread at startup.
    Runs 15 seconds after import to give the app time to finish initialising.
    """
    try:
        tmdb_api_key = get_setting('TMDB', 'api_key', '')
        if not tmdb_api_key:
            logging.info("[Prewarm] TMDB not configured, skipping discover cache prewarm")
            return

        logging.info("[Prewarm] Starting discover cache prewarm...")

        # ── Trending (movie, tv, anime) ────────────────────────────────────────
        for media_type, anime in [('movie', 'include'), ('tv', 'include'), ('tv', 'only')]:
            cache_key = f'{media_type}:{anime}'
            with _trending_cache_lock:
                if cache_key in _trending_cache and datetime.now() < _trending_cache[cache_key].get('expires', datetime.min):
                    continue
            try:
                if anime == 'only':
                    url = (f"https://api.themoviedb.org/3/discover/tv?api_key={tmdb_api_key}"
                           f"&sort_by=popularity.desc&page=1&with_keywords=210024")
                    resp = requests.get(url, timeout=15)
                    resp.raise_for_status()
                    raw = resp.json().get('results', [])[:20]
                    results = [{'id': t['id'], 'name': t.get('name', ''), 'overview': t.get('overview', ''),
                                'poster_path': t.get('poster_path'), 'backdrop_path': t.get('backdrop_path'),
                                'first_air_date': t.get('first_air_date', ''), 'vote_average': t.get('vote_average', 0),
                                'vote_count': t.get('vote_count', 0), 'media_type': 'tv',
                                'genre_ids': t.get('genre_ids', [])} for t in raw]
                else:
                    base = f"https://api.themoviedb.org/3/trending/{media_type}/week?api_key={tmdb_api_key}"
                    pages = 2 if media_type == 'tv' else 1
                    raw = []
                    for page in range(1, pages + 1):
                        resp = requests.get(f"{base}&page={page}", timeout=15)
                        resp.raise_for_status()
                        raw.extend(resp.json().get('results', []))
                    limit = 35 if media_type == 'tv' else 20
                    raw = raw[:limit]
                    if media_type == 'movie':
                        results = [{'id': i['id'], 'title': i.get('title', ''), 'overview': i.get('overview', ''),
                                    'poster_path': i.get('poster_path'), 'backdrop_path': i.get('backdrop_path'),
                                    'release_date': i.get('release_date', ''), 'vote_average': i.get('vote_average', 0),
                                    'vote_count': i.get('vote_count', 0), 'media_type': 'movie',
                                    'genre_ids': i.get('genre_ids', [])} for i in raw]
                    else:
                        results = [{'id': i['id'], 'name': i.get('name', ''), 'overview': i.get('overview', ''),
                                    'poster_path': i.get('poster_path'), 'backdrop_path': i.get('backdrop_path'),
                                    'first_air_date': i.get('first_air_date', ''), 'vote_average': i.get('vote_average', 0),
                                    'vote_count': i.get('vote_count', 0), 'media_type': 'tv',
                                    'genre_ids': i.get('genre_ids', [])} for i in raw]

                results = enrich_with_digital_dates(results, 'all', tmdb_api_key)
                add_db_status_and_episode_info(results, use_battery=False)
                with _trending_cache_lock:
                    _trending_cache[cache_key] = {'data': results, 'expires': datetime.now() + _TRENDING_TTL}
                logging.info(f"[Prewarm] Trending {cache_key}: {len(results)} items cached")
            except Exception as e:
                logging.warning(f"[Prewarm] Trending {cache_key} failed: {e}")

        # ── Trakt special lists ────────────────────────────────────────────────
        try:
            from content_checkers.trakt import get_trakt_config
            trakt_config = get_trakt_config()
            access_token = trakt_config.get('OAUTH_TOKEN', '')
            client_id = trakt_config.get('CLIENT_ID', '') or get_setting('Trakt', 'client_id', '')

            if access_token and client_id:
                trakt_headers = {
                    'Content-Type': 'application/json',
                    'trakt-api-version': '2',
                    'trakt-api-key': client_id,
                    'Authorization': f'Bearer {access_token}',
                }

                SPECIAL_ENDPOINTS = {
                    'trending':        {'movie': '/movies/trending',           'show': '/shows/trending'},
                    'popular':         {'movie': '/movies/popular',            'show': '/shows/popular'},
                    'favorited':       {'movie': '/movies/favorited/weekly',   'show': '/shows/favorited/weekly'},
                    'played':          {'movie': '/movies/played/weekly',      'show': '/shows/played/weekly'},
                    'watched':         {'movie': '/movies/watched/weekly',     'show': '/shows/watched/weekly'},
                    'collected':       {'movie': '/movies/collected/weekly',   'show': '/shows/collected/weekly'},
                    'anticipated':     {'movie': '/movies/anticipated',        'show': '/shows/anticipated'},
                    'boxoffice':       {'movie': '/movies/boxoffice',          'show': None},
                    'recommendations': {'movie': '/recommendations/movies',    'show': '/recommendations/shows'},
                }

                for list_type, endpoints in SPECIAL_ENDPOINTS.items():
                    cache_key = f'special:{list_type}:all'
                    with _trakt_cache_lock:
                        existing = _trakt_lists_cache.get(cache_key)
                    if existing and datetime.now() < existing.get('expires', datetime.min):
                        continue
                    try:
                        raw_items = []
                        for mt, ep in [('movie', endpoints.get('movie')), ('tv', endpoints.get('show'))]:
                            if not ep:
                                continue
                            r = requests.get(f'https://api.trakt.tv{ep}?limit=40&extended=full',
                                             headers=trakt_headers, timeout=20)
                            r.raise_for_status()
                            for item in r.json():
                                media_obj = item.get('movie') or item.get('show') or item
                                raw_items.append({'type': mt, ('movie' if mt == 'movie' else 'show'): media_obj})

                        tmdb_ids = [str((item.get('movie') or item.get('show') or {}).get('ids', {}).get('tmdb', ''))
                                    for item in raw_items]
                        tmdb_ids = [t for t in tmdb_ids if t]
                        content_hash = hashlib.md5(','.join(tmdb_ids).encode()).hexdigest()

                        if existing and existing.get('content_hash') == content_hash:
                            with _trakt_cache_lock:
                                _trakt_lists_cache[cache_key]['expires'] = datetime.now() + _TRAKT_SPECIAL_TTL
                            logging.info(f"[Prewarm] Special '{list_type}': unchanged, TTL extended")
                            continue

                        results = _trakt_items_to_discover(raw_items, 'all', tmdb_api_key)
                        with _trakt_cache_lock:
                            _trakt_lists_cache[cache_key] = {
                                'data': results,
                                'content_hash': content_hash,
                                'expires': datetime.now() + _TRAKT_SPECIAL_TTL,
                            }
                        logging.info(f"[Prewarm] Special '{list_type}': {len(results)} items cached")
                    except Exception as e:
                        logging.warning(f"[Prewarm] Special '{list_type}' failed: {e}")

                # ── Trakt my-list index ────────────────────────────────────────
                try:
                    with _trakt_cache_lock:
                        idx = _trakt_lists_cache.get('index')
                    if not idx or datetime.now() >= idx.get('expires', datetime.min):
                        r = requests.get('https://api.trakt.tv/users/me/lists',
                                         headers=trakt_headers, timeout=15)
                        r.raise_for_status()
                        lists = [{'name': l.get('name', ''), 'slug': l.get('ids', {}).get('slug', ''),
                                  'item_count': l.get('item_count', 0), 'description': l.get('description', ''),
                                  'updated_at': l.get('updated_at', '')} for l in r.json()]
                        with _trakt_cache_lock:
                            _trakt_lists_cache['index'] = {'data': lists,
                                                            'expires': datetime.now() + _TRAKT_LISTS_INDEX_TTL}
                        logging.info(f"[Prewarm] My-list index: {len(lists)} lists cached")
                    else:
                        lists = idx['data']
                        logging.debug(f"[Prewarm] My-list index already cached, using {len(lists)} lists")

                    # Prewarm each personal list (always runs regardless of index cache state)
                    for lst in lists:
                        slug = lst.get('slug', '')
                        if not slug:
                            continue
                        ml_key = f'{slug}:all'
                        with _trakt_cache_lock:
                            ml_entry = _trakt_lists_cache.get(ml_key)
                        if ml_entry and datetime.now() < ml_entry.get('expires', datetime.min):
                            logging.debug(f"[Prewarm] My list '{slug}' already cached, skipping")
                            continue
                        try:
                            raw_items = []
                            page = 1
                            while True:
                                pr = requests.get(
                                    f'https://api.trakt.tv/users/me/lists/{slug}/items?extended=full&limit=100&page={page}',
                                    headers=trakt_headers, timeout=20)
                                pr.raise_for_status()
                                page_items = pr.json()
                                if not page_items:
                                    break
                                raw_items.extend(page_items)
                                if page >= int(pr.headers.get('X-Pagination-Page-Count', 1)):
                                    break
                                page += 1
                            ml_results = _trakt_items_to_discover(raw_items, 'all', tmdb_api_key)
                            with _trakt_cache_lock:
                                _trakt_lists_cache[ml_key] = {
                                    'data': ml_results,
                                    'updated_at': lst.get('updated_at', ''),
                                    'expires': datetime.now() + _TRAKT_MYLIST_TTL,
                                }
                            _save_mylist_file_cache()
                            logging.info(f"[Prewarm] My list '{slug}': {len(ml_results)} items cached")
                        except Exception as e:
                            logging.warning(f"[Prewarm] My list '{slug}' failed: {e}")
                except Exception as e:
                    logging.warning(f"[Prewarm] My-list index failed: {e}")
            else:
                logging.info("[Prewarm] Trakt not configured, skipping special/my-list prewarm")
        except Exception as e:
            logging.warning(f"[Prewarm] Trakt prewarm failed: {e}")

        # ── FlixPatrol top10 (all platforms, type=all) ─────────────────────────
        try:
            _fp_consecutive_failures = 0
            for platform_key in ['netflix', 'disney', 'amazon', 'hbo', 'apple', 'paramount', 'hulu', 'peacock']:
                fp_cache_key = f'{platform_key}:all'
                with _flixpatrol_cache_lock:
                    fp_existing = _flixpatrol_cache.get(fp_cache_key)
                if fp_existing and datetime.now() < fp_existing.get('expires', datetime.min):
                    continue
                # Each fetch can trigger a real ~60s Cloudflare-challenge
                # browser-automation attempt when clearance isn't cached yet.
                # When FlixPatrol's bypass is failing outright, retrying it
                # identically for all 8 platforms burns 8+ minutes of CPU-heavy
                # browser automation on every single startup, starving every
                # other thread in this process (including the Checking queue)
                # via GIL contention for the whole process's lifetime.
                # Confirmed live: py-spy showed this thread pegging the process
                # at ~100% CPU for its entire 10+ minute uptime. Stop after 2
                # consecutive failures instead of blindly trying all 8.
                if _fp_consecutive_failures >= 2:
                    logging.warning("[Prewarm] Skipping remaining FlixPatrol platforms after repeated failures this run")
                    break
                try:
                    from concurrent.futures import ThreadPoolExecutor as _TPE
                    result = fetch_flixpatrol_top10(platform_key, media_type='all')
                    if 'error' in result:
                        _fp_consecutive_failures += 1
                        continue
                    _fp_consecutive_failures = 0
                    items = result.get('items', [])
                    enriched_items = []
                    if items:
                        def _enrich(item):
                            title = item.get('title', '')
                            mt = item.get('media_type', 'movie')
                            rank = item.get('rank')
                            search_types = ['movie', 'tv'] if mt == 'unknown' else [mt if mt == 'tv' else 'movie']
                            tmdb_id = None
                            for st in search_types:
                                try:
                                    sr = requests.get(
                                        f'https://api.themoviedb.org/3/search/{st}?api_key={tmdb_api_key}&query={requests.utils.quote(title)}&page=1',
                                        timeout=5)
                                    if sr.ok and sr.json().get('results'):
                                        tmdb_id = sr.json()['results'][0]['id']
                                        mt = st
                                        break
                                except Exception:
                                    pass
                            if not tmdb_id:
                                return None
                            try:
                                ep = 'tv' if mt == 'tv' else 'movie'
                                dr = requests.get(
                                    f'https://api.themoviedb.org/3/{ep}/{tmdb_id}?api_key={tmdb_api_key}&language=en-US',
                                    timeout=5)
                                if dr.ok:
                                    d = dr.json()
                                    return {
                                        'id': tmdb_id,
                                        'title': d.get('title') or d.get('name', ''),
                                        'overview': d.get('overview', ''),
                                        'poster_path': d.get('poster_path'),
                                        'backdrop_path': d.get('backdrop_path'),
                                        'release_date': d.get('release_date') or d.get('first_air_date', ''),
                                        'vote_average': d.get('vote_average', 0),
                                        'vote_count': d.get('vote_count', 0),
                                        'media_type': mt,
                                        'genre_ids': [g['id'] for g in d.get('genres', [])],
                                        'original_language': d.get('original_language', ''),
                                        'rank': rank,
                                    }
                            except Exception:
                                pass
                            return None

                        with _TPE(max_workers=10) as ex:
                            enriched_items = [r for r in ex.map(_enrich, items) if r is not None]
                        add_db_status_and_episode_info(enriched_items, use_battery=False)

                    response_data = {
                        'success': True,
                        'platform': result.get('platform'),
                        'platform_name': result.get('platform_name'),
                        'date': result.get('date'),
                        'results': enriched_items,
                        'total_results': len(enriched_items),
                    }
                    if enriched_items:
                        with _flixpatrol_cache_lock:
                            _flixpatrol_cache[fp_cache_key] = {
                                'data': response_data,
                                'expires': datetime.now() + _FLIXPATROL_TTL,
                            }
                    logging.info(f"[Prewarm] FlixPatrol {platform_key}: {len(enriched_items)} items cached")
                except Exception as e:
                    logging.warning(f"[Prewarm] FlixPatrol {platform_key} failed: {e}")
                    _fp_consecutive_failures += 1
        except Exception as e:
            logging.warning(f"[Prewarm] FlixPatrol prewarm failed: {e}")

        logging.info("[Prewarm] Discover cache prewarm complete")
    except Exception as e:
        logging.error(f"[Prewarm] Unexpected error during discover prewarm: {e}", exc_info=True)

# Fire 2 minutes after module import, after the busy startup window (initial
# sync, first page loads) instead of competing with it. Daemon, so it never holds the interpreter open: a non-daemon timer made every
# process that imports routes (restarts, scripts, each fresh-interpreter test)
# wait out the 15 seconds before it could exit.
_prewarm_timer = threading.Timer(120, _prewarm_discover_cache)

_prewarm_timer.daemon = True

_prewarm_timer.start()
