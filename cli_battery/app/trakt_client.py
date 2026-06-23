"""Trakt HTTP client — rate limiting, retries, auth.

Uses raw ``requests`` only (no ``trakt`` PyPI package).  All retries are
synchronous.  Rate limiting follows Trakt's documented limits:

- GET:  1000 calls per 5 minutes
- POST: 1 call per second
- Minimum 500 ms between any two requests (Cloudflare avoidance)
"""

import json
import time
import requests
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from .logger_config import logger
from . import trakt_auth

TRAKT_BASE_URL = "https://api.trakt.tv"
REQUEST_TIMEOUT = 10

# ─── Rate-limiting state (module-level, shared across all callers) ───────────
_get_times: deque = deque()
_post_times: deque = deque()
_last_request_time: float = 0.0
_MAX_GET_REQUESTS = 1000
_GET_WINDOW = 300  # 5 min
_MIN_INTERVAL = 0.5  # 500 ms — Cloudflare 429s below this in practice


def _check_rate_limit(method: str = 'GET') -> bool:
    global _last_request_time
    now = time.time()

    # Enforce minimum interval
    elapsed = now - _last_request_time
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)
        now = time.time()
    _last_request_time = now

    if method.upper() == 'GET':
        while _get_times and now - _get_times[0] > _GET_WINDOW:
            _get_times.popleft()
        if len(_get_times) >= _MAX_GET_REQUESTS:
            logger.warning(f"GET rate limit reached ({len(_get_times)} in {_GET_WINDOW}s)")
            return False
        _get_times.append(now)
    else:
        while _post_times and now - _post_times[0] > 1:
            _post_times.popleft()
        if len(_post_times) >= 1:
            logger.warning("POST rate limit reached")
            return False
        _post_times.append(now)
    return True


def _make_request(url: str, method: str = 'GET', max_retries: int = 4,
                  initial_delay: float = 5, extra_headers: dict | None = None):
    """Make an authenticated request to the Trakt API with retries."""
    if not trakt_auth.is_authenticated():
        logger.info("Not authenticated. Attempting token refresh.")
        if not trakt_auth.refresh_access_token():
            logger.error("Failed to refresh Trakt token — aborting request.")
            return None

    headers = trakt_auth.get_headers()
    if extra_headers:
        headers.update(extra_headers)

    delay = initial_delay
    for attempt in range(max_retries):
        if not _check_rate_limit(method):
            logger.warning("Internal rate limit hit — waiting 5 minutes")
            time.sleep(300)
            continue

        try:
            from utilities.trakt_coordinator import GlobalTraktCoordinator
            GlobalTraktCoordinator.get_instance().wait_if_needed()
        except Exception:
            pass

        try:
            resp = requests.request(method.upper(), url, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as e:
            logger.error(f"RequestException (attempt {attempt + 1}/{max_retries}) {url}: {e}")
            if attempt == max_retries - 1:
                return None
            time.sleep(delay)
            delay *= 2
            continue

        if resp.status_code == 404:
            return resp

        if resp.status_code == 401:
            logger.warning("401 — refreshing token")
            if trakt_auth.refresh_access_token():
                headers = trakt_auth.get_headers()
                continue
            logger.error("Token refresh failed after 401.")
            return None

        if resp.status_code == 429:
            retry_after = int(resp.headers.get('Retry-After', delay))
            logger.warning(f"Attempt {attempt + 1}/{max_retries} got 429 for {url}, backing off {retry_after}s")
            try:
                from utilities.trakt_coordinator import GlobalTraktCoordinator
                GlobalTraktCoordinator.get_instance().set_global_cooldown(retry_after)
            except Exception:
                pass
            if attempt < max_retries - 1:
                time.sleep(retry_after)
                continue
            return None

        if resp.status_code in (500, 502, 503, 504):
            logger.warning(f"Attempt {attempt + 1}/{max_retries} got {resp.status_code} for {url}")
            if attempt < max_retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            return None

        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError:
            logger.error(f"HTTP error {resp.status_code} for {url}: {resp.text}")
            return None
        return resp

    logger.error(f"Max retries reached for {url}")
    return None


# ─── Pagination helper ───────────────────────────────────────────────────────

def _fetch_paginated(url_base: str, since_iso: str | None = None) -> list:
    page = 1
    limit = 100
    results: list = []
    extra_headers: dict = {}
    if since_iso:
        try:
            dt_obj = datetime.fromisoformat(since_iso.replace('Z', '+00:00'))
            extra_headers['X-Start-Date'] = dt_obj.strftime('%Y-%m-%d')
        except Exception:
            pass

    while True:
        url = f"{url_base}?page={page}&limit={limit}"
        resp = _make_request(url, extra_headers=extra_headers)
        if not resp or resp.status_code != 200:
            break
        data = resp.json() or []
        if not isinstance(data, list):
            break
        results.extend(data)
        try:
            page_count = int(resp.headers.get('X-Pagination-Page-Count', '1'))
        except ValueError:
            page_count = 1
        if page >= page_count or not data:
            break
        page += 1
    return results


def _format_start_date(since_iso: str) -> str:
    try:
        dt_obj = datetime.fromisoformat(since_iso.replace('Z', '+00:00'))
        rounded = dt_obj.replace(minute=0, second=0, microsecond=0)
        return rounded.strftime('%Y-%m-%dT%H:%M:%SZ')
    except Exception:
        fallback = datetime.now(timezone.utc) - timedelta(days=1)
        return fallback.replace(minute=0, second=0, microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ')


# ─── Public API ──────────────────────────────────────────────────────────────

def search_by_imdb(imdb_id: str):
    """Search Trakt by IMDb ID, return first result dict or None."""
    url = f"{TRAKT_BASE_URL}/search/imdb/{imdb_id}?type=show,movie"
    resp = _make_request(url)
    if resp and resp.status_code == 200:
        results = resp.json()
        if results:
            return results[0]
    return None


def get_show_data(imdb_id: str) -> Optional[dict]:
    """Get full show metadata (extended=full) + aliases + seasons/episodes."""
    # Trakt accepts IMDb IDs directly — no search step needed
    url = f"{TRAKT_BASE_URL}/shows/{imdb_id}?extended=full"
    resp = _make_request(url)
    if not resp or resp.status_code != 200:
        return None

    show_data = resp.json()
    if show_data.get('ids', {}).get('imdb') != imdb_id:
        logger.error(f"IMDb mismatch: requested {imdb_id}, got {show_data.get('ids', {}).get('imdb')}")
        return None

    # Aliases
    slug = show_data.get('ids', {}).get('slug')
    tmdb_id = show_data.get('ids', {}).get('tmdb')
    trakt_title = show_data.get('title', '')
    if slug:
        aliases = get_show_aliases(slug) or {}
        # Enrich with TMDB alternative titles + primary TMDB title if different from Trakt
        tmdb_alts = _fetch_tmdb_alternative_titles(tmdb_id, 'tv', trakt_title)
        aliases = _merge_tmdb_aliases(aliases, tmdb_alts)
        if aliases:
            show_data['aliases'] = aliases

    # Seasons + episodes
    seasons, _ = get_show_seasons_and_episodes(imdb_id, include_specials=True)
    show_data['seasons'] = seasons

    return show_data


def get_show_seasons_and_episodes(imdb_id: str, include_specials: bool = False) -> Tuple[Optional[dict], Optional[str]]:
    url = f"{TRAKT_BASE_URL}/shows/{imdb_id}/seasons?extended=full,episodes"
    resp = _make_request(url)
    if not resp or resp.status_code != 200:
        return None, None

    raw = resp.json()
    processed: dict = {}
    for season in raw:
        season_number = season.get('number')
        if season_number is None:
            continue
        if not include_specials and season_number == 0:
            continue

        episodes = season.get('episodes', [])
        ep_dict: dict = {}
        for ep in episodes:
            ep_num = ep.get('number')
            if ep_num is None:
                continue
            ep_plex = ep.get('ids', {}).get('plex') or {}
            ep_ids = ep.get('ids', {}) or {}
            ep_dict[ep_num] = {
                'title': ep.get('title', ''),
                'overview': ep.get('overview', ''),
                'runtime': ep.get('runtime', 0),
                'first_aired': ep.get('first_aired'),
                'imdb_id': ep_ids.get('imdb'),
                'tmdb_id': str(ep_ids['tmdb']) if ep_ids.get('tmdb') else None,
                'tvdb_id': str(ep_ids['tvdb']) if ep_ids.get('tvdb') else None,
                'absolute': ep.get('number_abs'),
                'plex_guid': ep_plex.get('guid') if isinstance(ep_plex, dict) else None,
            }
        season_plex = season.get('ids', {}).get('plex') or {}
        season_ids = season.get('ids', {}) or {}
        processed[season_number] = {
            'episode_count': season.get('episode_count', len(episodes)),
            'episodes': ep_dict,
            'plex_guid': season_plex.get('guid') if isinstance(season_plex, dict) else None,
            'tmdb_id': str(season_ids['tmdb']) if season_ids.get('tmdb') else None,
            'tvdb_id': str(season_ids['tvdb']) if season_ids.get('tvdb') else None,
        }
    return processed, 'trakt'


def get_show_aliases(slug: str) -> Optional[dict]:
    url = f"{TRAKT_BASE_URL}/shows/{slug}/aliases"
    resp = _make_request(url)
    if resp and resp.status_code == 200:
        aliases: defaultdict = defaultdict(list)
        for item in resp.json():
            country = item.get('country', 'unknown')
            title = item.get('title')
            if title:
                aliases[country].append(title)
        return dict(aliases)
    return None


def get_movie_data(imdb_id: str) -> Optional[dict]:
    """Get full movie metadata (extended=full) + aliases + release dates."""
    url = f"{TRAKT_BASE_URL}/movies/{imdb_id}?extended=full"
    resp = _make_request(url)
    if not resp or resp.status_code != 200:
        return None

    data = resp.json()
    if data.get('ids', {}).get('imdb') != imdb_id:
        logger.error(f"IMDb mismatch: requested {imdb_id}, got {data.get('ids', {}).get('imdb')}")
        return None

    slug = data.get('ids', {}).get('slug')
    tmdb_id = data.get('ids', {}).get('tmdb')
    trakt_title = data.get('title', '')
    if slug:
        aliases = get_movie_aliases(slug) or {}
        # Enrich with TMDB alternative titles + primary TMDB title if different from Trakt
        tmdb_alts = _fetch_tmdb_alternative_titles(tmdb_id, 'movie', trakt_title)
        aliases = _merge_tmdb_aliases(aliases, tmdb_alts)
        if aliases:
            data['aliases'] = aliases

        releases = _fetch_movie_releases(slug, imdb_id)
        if releases:
            data['release_dates'] = releases

    # Fallback: if Trakt has no year, extract from release_dates
    if not data.get('year') and data.get('release_dates'):
        earliest = None
        for country_dates in data['release_dates'].values():
            for rd in (country_dates if isinstance(country_dates, list) else []):
                d = (rd.get('date') or rd.get('release_date') or '')[:10]
                if d and len(d) >= 4:
                    try:
                        y = int(d[:4])
                        if earliest is None or y < earliest:
                            earliest = y
                    except (ValueError, TypeError):
                        pass
        if earliest:
            data['year'] = earliest

    return data


def _fetch_tmdb_alternative_titles(tmdb_id, media_type: str, trakt_title: str = '') -> dict:
    """Fetch English alternative titles from TMDB and return as {country: [titles]} dict.

    Also includes the TMDB primary title if it differs from the Trakt title
    (e.g. Trakt has "187" but TMDB has "One Eight Seven").

    Backwards-compatible: returns empty dict on any failure (missing API key,
    network error, TMDB unavailable). Never raises.
    media_type: 'movie' or 'tv'
    """
    try:
        from utilities.settings import get_setting as _gs
        api_key = _gs('TMDB', 'api_key', '')
        if not api_key or not tmdb_id:
            return {}
        result: defaultdict = defaultdict(list)

        # Single TMDB call with append_to_response for efficiency
        url = (f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}"
               f"?api_key={api_key}&append_to_response=alternative_titles")
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return {}
        data = resp.json()

        # Primary TMDB title — add as 'us' alias if different from Trakt title
        for field in ('title', 'name'):
            tmdb_title = data.get(field, '')
            if tmdb_title and tmdb_title.lower() != (trakt_title or '').lower():
                result['us'].append(tmdb_title)
                break

        # Alternative titles — movies use 'titles', TV shows use 'results'
        alt_data = data.get('alternative_titles') or {}
        items = alt_data.get('titles') or alt_data.get('results') or []
        for item in items:
            iso = (item.get('iso_3166_1') or '').lower()
            title = item.get('title') or item.get('name') or ''
            if title and iso:
                result[iso].append(title)

        return dict(result)
    except Exception:
        return {}


def _merge_tmdb_aliases(aliases: dict, tmdb_alts: dict) -> dict:
    """Merge TMDB alternative titles into existing Trakt aliases dict.

    Only adds titles not already present. Returns merged dict.
    Backwards-compatible: if tmdb_alts is empty, returns aliases unchanged.
    """
    if not tmdb_alts:
        return aliases
    merged: defaultdict = defaultdict(list, {k: list(v) for k, v in aliases.items()})
    for country, titles in tmdb_alts.items():
        existing_lower = {t.lower() for t in merged[country]}
        for title in titles:
            if title and title.lower() not in existing_lower:
                merged[country].append(title)
                existing_lower.add(title.lower())
    return dict(merged)


def get_movie_aliases(slug: str) -> Optional[dict]:
    url = f"{TRAKT_BASE_URL}/movies/{slug}/aliases"
    resp = _make_request(url)
    if resp and resp.status_code == 200:
        aliases: defaultdict = defaultdict(list)
        for item in resp.json():
            country = item.get('country', 'unknown')
            title = item.get('title')
            if title:
                aliases[country].append(title)
        return dict(aliases)
    return None


def _fetch_movie_releases(slug: str, imdb_id: str) -> Optional[dict]:
    url = f"{TRAKT_BASE_URL}/movies/{slug}/releases"
    resp = _make_request(url)
    if not resp or resp.status_code != 200 or not resp.json():
        return None

    formatted: defaultdict = defaultdict(list)
    for release in resp.json():
        country = release.get('country')
        release_date_str = release.get('release_date')
        release_type = release.get('release_type')
        if country and release_date_str:
            try:
                dt_obj = datetime.fromisoformat(release_date_str.replace('Z', '+00:00'))
                try:
                    from metadata.metadata import _get_local_timezone
                    dt_obj = dt_obj.astimezone(_get_local_timezone())
                except ImportError:
                    pass
                formatted[country].append({
                    'date': dt_obj.date().isoformat(),
                    'type': release_type,
                })
            except Exception as e:
                logger.warning(f"Could not parse release date {release_date_str} for {imdb_id}: {e}")
    return dict(formatted)


def get_movie_release_dates(imdb_id: str) -> Optional[dict]:
    """Fetch release dates by first finding the slug via search."""
    result = search_by_imdb(imdb_id)
    if not result or result.get('type') != 'movie':
        return None
    slug = result.get('movie', {}).get('ids', {}).get('slug')
    if not slug:
        return None
    return _fetch_movie_releases(slug, imdb_id)


def convert_tmdb_to_imdb(tmdb_id: str, media_type: str | None = None) -> Tuple[Optional[str], Optional[str]]:
    url = f"{TRAKT_BASE_URL}/search/tmdb/{tmdb_id}?type=movie,show"
    resp = _make_request(url)
    if not resp or resp.status_code != 200:
        return None, None

    data = resp.json()
    if not data:
        return None, None

    for item in data:
        if media_type:
            if media_type == 'show' and 'show' in item:
                return item['show']['ids'].get('imdb'), 'trakt'
            if media_type == 'movie' and 'movie' in item:
                return item['movie']['ids'].get('imdb'), 'trakt'
        else:
            if 'show' in item:
                return item['show']['ids'].get('imdb'), 'trakt'
            if 'movie' in item:
                return item['movie']['ids'].get('imdb'), 'trakt'
    return None, None


def search_media(query: str, year: Optional[int] = None,
                 media_type: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    search_type = media_type if media_type in ('movie', 'show') else 'movie,show'
    params = {'query': query, 'extended': 'full'}
    url = f"{TRAKT_BASE_URL}/search/{search_type}?{urlencode(params)}"

    resp = _make_request(url)
    if not resp or resp.status_code != 200:
        return None

    try:
        raw = resp.json()
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(raw, list):
        return None

    results: list = []
    for item_raw in raw:
        item_type = item_raw.get('type')
        item_data = item_raw.get(item_type)
        if not item_type or not item_data or not isinstance(item_data, dict):
            continue
        if year and item_data.get('year') != year:
            continue
        ids = item_data.get('ids', {})
        results.append({
            'title': item_data.get('title'),
            'year': item_data.get('year'),
            'imdb_id': ids.get('imdb'),
            'tmdb_id': ids.get('tmdb'),
            'type': item_type,
        })
    return results


def get_plex_guid(imdb_id: str, media_type: str) -> Optional[Dict[str, Any]]:
    """
    Fetch Plex GUIDs for a movie or show from Trakt.

    For movies: returns {'show_guid': str|None}
    For shows:  returns {
        'show_guid': str|None,
        'season_guids': {season_num: guid, ...}|None,
        'episode_guids': {season_num: {ep_num: guid, ...}, ...}|None,
    }

    Uses ?extended=full,episodes on the seasons endpoint to get all levels
    in a single call. Called as supplementary lookup when TVDB is primary.
    """
    if media_type == 'movie':
        url = f"{TRAKT_BASE_URL}/movies/{imdb_id}?extended=full"
        resp = _make_request(url)
        if resp and resp.status_code == 200:
            plex = resp.json().get('ids', {}).get('plex') or {}
            return {'show_guid': plex.get('guid') if isinstance(plex, dict) else None}
        return None

    # TV show — fetch show-level GUID
    url = f"{TRAKT_BASE_URL}/shows/{imdb_id}?extended=full"
    resp = _make_request(url)
    if not resp or resp.status_code != 200:
        return None

    plex = resp.json().get('ids', {}).get('plex') or {}
    show_guid = plex.get('guid') if isinstance(plex, dict) else None

    # Fetch season + episode GUIDs in one call (?extended=full,episodes)
    seasons_url = f"{TRAKT_BASE_URL}/shows/{imdb_id}/seasons?extended=full,episodes"
    seasons_resp = _make_request(seasons_url)
    season_guids: Dict[int, str] = {}
    season_tmdb_ids: Dict[int, str] = {}
    season_tvdb_ids: Dict[int, str] = {}
    episode_guids: Dict[int, Dict[int, str]] = {}
    episode_imdb_ids: Dict[int, Dict[int, str]] = {}
    episode_tmdb_ids: Dict[int, Dict[int, str]] = {}
    episode_tvdb_ids: Dict[int, Dict[int, str]] = {}

    if seasons_resp and seasons_resp.status_code == 200:
        for season in seasons_resp.json():
            snum = season.get('number')
            if snum is None:
                continue
            s_ids = season.get('ids', {}) or {}
            s_plex = s_ids.get('plex') or {}
            s_guid = s_plex.get('guid') if isinstance(s_plex, dict) else None
            if s_guid:
                season_guids[snum] = s_guid
            if s_ids.get('tmdb'):
                season_tmdb_ids[snum] = str(s_ids['tmdb'])
            if s_ids.get('tvdb'):
                season_tvdb_ids[snum] = str(s_ids['tvdb'])

            for ep in season.get('episodes', []):
                ep_num = ep.get('number')
                if ep_num is None:
                    continue
                ep_ids = ep.get('ids', {}) or {}
                ep_plex = ep_ids.get('plex') or {}
                ep_guid = ep_plex.get('guid') if isinstance(ep_plex, dict) else None
                if ep_guid:
                    episode_guids.setdefault(snum, {})[ep_num] = ep_guid
                if ep_ids.get('imdb'):
                    episode_imdb_ids.setdefault(snum, {})[ep_num] = ep_ids['imdb']
                if ep_ids.get('tmdb'):
                    episode_tmdb_ids.setdefault(snum, {})[ep_num] = str(ep_ids['tmdb'])
                if ep_ids.get('tvdb'):
                    episode_tvdb_ids.setdefault(snum, {})[ep_num] = str(ep_ids['tvdb'])

    return {
        'show_guid': show_guid,
        'season_guids': season_guids or None,
        'season_tmdb_ids': season_tmdb_ids or None,
        'season_tvdb_ids': season_tvdb_ids or None,
        'episode_guids': episode_guids or None,
        'episode_imdb_ids': episode_imdb_ids or None,
        'episode_tmdb_ids': episode_tmdb_ids or None,
        'episode_tvdb_ids': episode_tvdb_ids or None,
    }


def get_updated_shows(since_iso: str) -> List[dict]:
    start_date = _format_start_date(since_iso)
    url_base = f"{TRAKT_BASE_URL}/shows/updates/{start_date}"
    items = _fetch_paginated(url_base, since_iso)
    results: list = []
    for entry in items:
        show_obj = entry.get('show') if isinstance(entry, dict) else None
        if not show_obj:
            continue
        ids = show_obj.get('ids', {})
        imdb_id = ids.get('imdb')
        updated_at = entry.get('updated_at') or show_obj.get('updated_at')
        if imdb_id and updated_at:
            results.append({'imdb_id': imdb_id, 'updated_at': updated_at})
    logger.info(f"Fetched {len(results)} updated shows since {start_date}")
    return results


def get_updated_movies(since_iso: str) -> List[dict]:
    start_date = _format_start_date(since_iso)
    url_base = f"{TRAKT_BASE_URL}/movies/updates/{start_date}"
    items = _fetch_paginated(url_base, since_iso)
    results: list = []
    for entry in items:
        movie_obj = entry.get('movie') if isinstance(entry, dict) else None
        if not movie_obj:
            continue
        ids = movie_obj.get('ids', {})
        imdb_id = ids.get('imdb')
        updated_at = entry.get('updated_at') or movie_obj.get('updated_at')
        if imdb_id and updated_at:
            results.append({'imdb_id': imdb_id, 'updated_at': updated_at})
    logger.info(f"Fetched {len(results)} updated movies since {start_date}")
    return results
