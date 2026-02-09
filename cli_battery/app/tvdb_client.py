"""TVDB v4 API client — drop-in alternative to trakt_client.

When a TVDB API key is configured, this module provides the same public API
surface as trakt_client so callers can swap between them transparently.

Auth: POST /login with {"apikey": "..."} → bearer token (valid ~28 days).
Rate limiting: No preemptive tracking; 429s handled with exponential backoff.
"""

import json
import time
import logging
import threading
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, quote

import requests

from .logger_config import logger
from .database import (
    Session as DbSession, managed_session,
    TVDBToIMDBMapping, DatabaseManager,
)

TVDB_BASE_URL = "https://api4.thetvdb.com/v4"
REQUEST_TIMEOUT = 15

# Module-level token state (same thread-safety pattern as trakt_client)
_token: str | None = None
_token_lock = threading.Lock()

# TVDB 3-letter language codes → ISO 639-1 2-letter country/language codes
_LANG3_TO_LANG2 = {
    'eng': 'us', 'jpn': 'jp', 'kor': 'kr', 'zho': 'cn', 'cmn': 'cn',
    'fra': 'fr', 'deu': 'de', 'spa': 'es', 'ita': 'it', 'por': 'pt',
    'rus': 'ru', 'ara': 'ar', 'hin': 'hi', 'tha': 'th', 'vie': 'vi',
    'pol': 'pl', 'nld': 'nl', 'swe': 'sv', 'dan': 'da', 'nor': 'no',
    'fin': 'fi', 'ces': 'cz', 'hun': 'hu', 'ron': 'ro', 'tur': 'tr',
    'heb': 'il', 'ell': 'gr', 'ukr': 'ua', 'ind': 'id', 'msa': 'my',
    'kat': 'ge', 'bul': 'bg', 'hrv': 'hr', 'srp': 'rs', 'slk': 'sk',
    'slv': 'si', 'lit': 'lt', 'lav': 'lv', 'est': 'ee', 'tgl': 'ph',
    'tam': 'in', 'tel': 'in', 'ben': 'bd', 'urd': 'pk', 'fas': 'ir',
    'kan': 'in', 'mal': 'in', 'mar': 'in', 'pan': 'in', 'guj': 'in',
    'pt-BR': 'br', 'zho-TW': 'tw', 'yue': 'hk',
}

# TVDB status → Trakt-compatible status strings (must match staleness.py expectations)
_STATUS_MAP = {
    'Continuing': 'returning series',
    'Ended': 'ended',
    'Upcoming': 'in production',
    'To Be Determined': 'returning series',
    'Cancelled': 'canceled',
    'Pilot Ordered': 'in production',
    'In Development': 'planned',
    'Released': 'released',
    'Post Production': 'post production',
    'In Production': 'in production',
    'Planned': 'planned',
}


def is_available() -> bool:
    """Check if TVDB API key is configured."""
    try:
        from utilities.settings import get_setting
        key = get_setting('TVDB', 'api_key', default='')
        return bool(key and key.strip())
    except Exception:
        return False


def _ensure_token() -> bool:
    """Authenticate with TVDB and cache the bearer token. Returns True on success."""
    global _token
    with _token_lock:
        if _token:
            return True
        try:
            from utilities.settings import get_setting
            api_key = get_setting('TVDB', 'api_key', default='')
            if not api_key:
                return False

            resp = requests.post(
                f"{TVDB_BASE_URL}/login",
                json={"apikey": api_key},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                data = resp.json()
                _token = data.get('data', {}).get('token') or data.get('token')
                if _token:
                    logger.info("TVDB authentication successful")
                    return True
                logger.error("TVDB login response missing token")
                return False
            else:
                logger.error(f"TVDB authentication failed: HTTP {resp.status_code}")
                return False
        except Exception as e:
            logger.error(f"TVDB authentication error: {e}")
            return False


def _make_request(url: str, max_retries: int = 4, initial_delay: float = 5) -> Optional[requests.Response]:
    """GET request with retry/backoff on 429, 401 re-auth, 5xx retry."""
    global _token

    if not _ensure_token():
        return None

    delay = initial_delay
    for attempt in range(max_retries):
        try:
            resp = requests.get(
                url,
                headers={"Authorization": f"Bearer {_token}"},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.exceptions.RequestException as e:
            logger.warning(f"TVDB request error for {url}: {e}")
            if attempt < max_retries - 1:
                time.sleep(delay)
                delay *= 2
            continue

        if resp.status_code == 200:
            return resp

        if resp.status_code == 404:
            return resp

        if resp.status_code == 401:
            logger.warning("TVDB 401 — refreshing token")
            with _token_lock:
                _token = None
            if _ensure_token() and attempt < max_retries - 1:
                continue
            return None

        if resp.status_code == 429:
            retry_after = int(resp.headers.get('Retry-After', delay))
            logger.warning(f"TVDB 429 for {url}, backing off {retry_after}s (attempt {attempt + 1}/{max_retries})")
            time.sleep(retry_after)
            delay *= 2
            continue

        if resp.status_code in (500, 502, 503, 504):
            logger.warning(f"TVDB {resp.status_code} for {url} (attempt {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            return None

        logger.error(f"TVDB unexpected HTTP {resp.status_code} for {url}")
        return None

    return None


def _resolve_tvdb_id(imdb_id: str, media_type: str = None) -> Optional[int]:
    """IMDb → TVDB ID, cached in TVDBToIMDBMapping table."""
    # Check cache first
    with managed_session() as session:
        mapping = session.query(TVDBToIMDBMapping).filter_by(imdb_id=imdb_id).first()
        if mapping:
            return int(mapping.tvdb_id)

    # Fetch from TVDB
    url = f"{TVDB_BASE_URL}/search/remoteid/{imdb_id}"
    resp = _make_request(url)
    if not resp or resp.status_code != 200:
        return None

    data = resp.json().get('data', [])
    if not data:
        return None

    # Response items are nested: {"series": {"id": ...}} or {"movie": {"id": ...}}
    # Extract the inner object and determine the type from the wrapper key.
    for item in data:
        for key in ('series', 'movie'):
            inner = item.get(key)
            if isinstance(inner, dict) and inner.get('id'):
                tvdb_id = inner['id']
                detected_type = 'show' if key == 'series' else 'movie'

                if media_type:
                    if media_type == 'show' and detected_type == 'show':
                        _cache_tvdb_mapping(str(tvdb_id), imdb_id, 'show')
                        return int(tvdb_id)
                    elif media_type == 'movie' and detected_type == 'movie':
                        _cache_tvdb_mapping(str(tvdb_id), imdb_id, 'movie')
                        return int(tvdb_id)
                else:
                    _cache_tvdb_mapping(str(tvdb_id), imdb_id, detected_type)
                    return int(tvdb_id)

        # Fallback: top-level id (some endpoints may return flat structure)
        if item.get('id') and not item.get('series') and not item.get('movie'):
            tvdb_id = item['id']
            item_type = item.get('type', '').lower()
            detected_type = 'movie' if item_type == 'movie' else 'show'
            if not media_type or media_type == detected_type:
                _cache_tvdb_mapping(str(tvdb_id), imdb_id, detected_type)
                return int(tvdb_id)

    # Last resort: take the first result with any ID
    for item in data:
        for key in ('series', 'movie'):
            inner = item.get(key)
            if isinstance(inner, dict) and inner.get('id'):
                tvdb_id = inner['id']
                detected_type = 'show' if key == 'series' else 'movie'
                _cache_tvdb_mapping(str(tvdb_id), imdb_id, detected_type)
                return int(tvdb_id)

    return None


def _cache_tvdb_mapping(tvdb_id: str, imdb_id: str, media_type: str):
    """Cache a TVDB→IMDb mapping in the database."""
    try:
        DatabaseManager.add_tvdb_to_imdb_mapping(tvdb_id, imdb_id, media_type)
    except Exception as e:
        logger.debug(f"Could not cache TVDB mapping {tvdb_id}→{imdb_id}: {e}")


def _convert_lang3_to_lang2(lang3: str) -> str:
    """Convert 3-letter language code to 2-letter country code."""
    if not lang3:
        return 'unknown'
    return _LANG3_TO_LANG2.get(lang3, lang3[:2].lower() if len(lang3) >= 2 else 'unknown')


def _extract_aliases(translations: list) -> dict:
    """Convert TVDB nameTranslations to country-keyed alias dict."""
    aliases: defaultdict = defaultdict(list)
    if not translations:
        return {}
    for t in translations:
        lang = t.get('language', '')
        name = t.get('name', '')
        if name:
            country = _convert_lang3_to_lang2(lang)
            if name not in aliases[country]:
                aliases[country].append(name)
    return dict(aliases)


def _format_air_date(date_str: str | None, airs_time: str | None = None,
                     airs_timezone: str | None = None) -> str | None:
    """Convert TVDB 'YYYY-MM-DD' to ISO 8601 UTC datetime.

    TVDB episode dates are in the show's local timezone (date-only).  When
    *airs_time* (HH:MM) and *airs_timezone* (IANA tz name) are provided we
    combine them with the date to produce a proper UTC timestamp, matching what
    Trakt returns.  Otherwise we fall back to midnight UTC.
    """
    if not date_str:
        return None
    # Already ISO 8601 format with time component
    if 'T' in date_str:
        return date_str

    if airs_time and airs_timezone:
        try:
            from zoneinfo import ZoneInfo
            # Parse "HH:MM" (or "HH:MM:SS")
            parts = airs_time.split(':')
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
            show_tz = ZoneInfo(airs_timezone)
            naive_dt = datetime.strptime(date_str[:10], '%Y-%m-%d').replace(
                hour=hour, minute=minute)
            local_dt = naive_dt.replace(tzinfo=show_tz)
            utc_dt = local_dt.astimezone(timezone.utc)
            return utc_dt.strftime('%Y-%m-%dT%H:%M:%S.000Z')
        except Exception:
            pass  # Fall through to midnight UTC

    # Fallback — no airs info available
    return f"{date_str}T00:00:00.000Z"


def _map_status(tvdb_status: str | None) -> str | None:
    """Map TVDB status string to Trakt-compatible status."""
    if not tvdb_status:
        return None
    return _STATUS_MAP.get(tvdb_status, tvdb_status.lower())


# ─── Public API (matches trakt_client signatures) ────────────────────────────


def get_show_data(imdb_id: str) -> Optional[dict]:
    """Get full show metadata + aliases + seasons/episodes."""
    tvdb_id = _resolve_tvdb_id(imdb_id, media_type='show')
    if not tvdb_id:
        logger.warning(f"TVDB: could not resolve IMDb {imdb_id} to TVDB ID, trying TMDB fallback")
        tmdb_api_key = _get_tmdb_api_key()
        if tmdb_api_key:
            return _fetch_tmdb_show_data(imdb_id, tmdb_api_key)
        return None

    url = f"{TVDB_BASE_URL}/series/{tvdb_id}/extended?meta=translations,episodes"
    resp = _make_request(url)
    if not resp or resp.status_code != 200:
        return None

    raw = resp.json().get('data', {})
    if not raw:
        return None

    # Build Trakt-compatible show dict
    show_data = _build_show_dict(raw, imdb_id, tvdb_id)

    # Aliases from translations
    translations = raw.get('translations', {}).get('nameTranslations', [])
    aliases = _extract_aliases(translations)
    if aliases:
        show_data['aliases'] = aliases

    # Seasons + episodes
    seasons = _extract_seasons_from_extended(raw)
    if seasons is None:
        # Fallback: separate seasons/episodes call
        seasons, _ = get_show_seasons_and_episodes(imdb_id, include_specials=True)
    show_data['seasons'] = seasons

    return show_data


def _build_show_dict(raw: dict, imdb_id: str, tvdb_id: int) -> dict:
    """Build a Trakt-compatible show metadata dict from TVDB series data."""
    ids = {
        'imdb': imdb_id,
        'tvdb': tvdb_id,
        'slug': raw.get('slug', ''),
    }

    # Extract TMDB ID from remoteIds
    remote_ids = raw.get('remoteIds', []) or raw.get('remote_ids', [])
    if isinstance(remote_ids, list):
        for rid in remote_ids:
            if isinstance(rid, dict):
                source = (rid.get('sourceName', '') or '').lower()
                if 'tmdb' in source or 'themoviedb' in source:
                    try:
                        ids['tmdb'] = int(rid.get('id', ''))
                    except (ValueError, TypeError):
                        pass
                    break

    # Extract year from firstAired
    year = None
    first_aired = raw.get('firstAired', '')
    if first_aired and len(first_aired) >= 4:
        try:
            year = int(first_aired[:4])
        except ValueError:
            pass

    # Find English overview, fallback to default
    overview = raw.get('overview', '')
    for t in raw.get('translations', {}).get('overviewTranslations', []):
        if t.get('language') == 'eng':
            overview = t.get('overview', overview)
            break

    # Find English title, fallback to name
    title = raw.get('name', '')
    for t in raw.get('translations', {}).get('nameTranslations', []):
        if t.get('language') == 'eng':
            title = t.get('name', title)
            break

    # Airs info
    airs = {}
    if raw.get('airsDays'):
        days = raw['airsDays']
        for day, val in days.items():
            if val:
                airs['day'] = day.capitalize()
                break
    if raw.get('airsTime'):
        airs['time'] = raw['airsTime']
    if raw.get('originalNetwork'):
        airs['timezone'] = raw['originalNetwork'].get('timezone', 'America/New_York')

    show_data = {
        'title': title,
        'year': year,
        'ids': ids,
        'overview': overview,
        'runtime': raw.get('averageRuntime') or raw.get('defaultSeasonType'),
        'status': _map_status(raw.get('status', {}).get('name') if isinstance(raw.get('status'), dict) else raw.get('status')),
        'network': raw.get('originalNetwork', {}).get('name', '') if isinstance(raw.get('originalNetwork'), dict) else '',
        'genres': [g.get('name', '') for g in raw.get('genres', []) if isinstance(g, dict)],
        'type': 'show',
    }

    if airs:
        show_data['airs'] = airs

    # Rating
    if raw.get('score'):
        show_data['rating'] = raw['score']

    return show_data


def _extract_seasons_from_extended(raw: dict) -> Optional[dict]:
    """Extract seasons/episodes from an extended series response."""
    seasons_raw = raw.get('seasons', [])
    episodes_raw = raw.get('episodes', [])

    if not seasons_raw:
        return None

    # Extract show-level airs info for proper datetime construction
    airs_time = raw.get('airsTime')  # e.g. "20:00"
    network = raw.get('originalNetwork')
    airs_timezone = network.get('timezone', 'America/New_York') if isinstance(network, dict) else None

    # Group episodes by season number
    eps_by_season: defaultdict = defaultdict(list)
    if episodes_raw:
        for ep in episodes_raw:
            if isinstance(ep, dict):
                sn = ep.get('seasonNumber')
                if sn is not None:
                    eps_by_season[sn].append(ep)

    processed: dict = {}
    for season in seasons_raw:
        if not isinstance(season, dict):
            continue
        season_number = season.get('number')
        if season_number is None:
            continue

        # Get season type - only include "Aired Order" seasons (type id 1) or default
        season_type = season.get('type', {})
        if isinstance(season_type, dict):
            type_id = season_type.get('id')
            # Type 1 = Aired Order, skip alternate/DVD order seasons
            if type_id is not None and type_id != 1:
                continue

        season_episodes = eps_by_season.get(season_number, [])
        ep_dict: dict = {}
        for ep in season_episodes:
            ep_num = ep.get('number')
            if ep_num is None:
                continue
            ep_dict[ep_num] = {
                'title': ep.get('name', ''),
                'overview': ep.get('overview', ''),
                'runtime': ep.get('runtime', 0),
                'first_aired': _format_air_date(ep.get('aired'), airs_time, airs_timezone),
                'imdb_id': None,  # TVDB doesn't provide per-episode IMDb IDs
                'absolute': ep.get('absoluteNumber'),
            }

        processed[season_number] = {
            'episode_count': len(season_episodes) if season_episodes else 0,
            'episodes': ep_dict,
        }

    return processed if processed else None


def get_show_seasons_and_episodes(imdb_id: str, include_specials: bool = False) -> Tuple[Optional[dict], Optional[str]]:
    """Fetch seasons and episodes for a show."""
    tvdb_id = _resolve_tvdb_id(imdb_id, media_type='show')
    if not tvdb_id:
        return None, None

    # Try extended endpoint first
    url = f"{TVDB_BASE_URL}/series/{tvdb_id}/extended?meta=episodes"
    resp = _make_request(url)
    if not resp or resp.status_code != 200:
        return None, None

    raw = resp.json().get('data', {})
    seasons = _extract_seasons_from_extended(raw)

    # Extract airs info for paginated fallback
    airs_time = raw.get('airsTime')
    network = raw.get('originalNetwork')
    airs_timezone = network.get('timezone', 'America/New_York') if isinstance(network, dict) else None

    if not seasons:
        # Fallback: paginated episodes endpoint
        seasons = _fetch_episodes_paginated(tvdb_id, airs_time, airs_timezone)

    if seasons and not include_specials:
        seasons.pop(0, None)

    return seasons, 'tvdb' if seasons else None


def _fetch_episodes_paginated(tvdb_id: int, airs_time: str | None = None,
                              airs_timezone: str | None = None) -> Optional[dict]:
    """Fetch episodes via the paginated /episodes/default endpoint."""
    all_episodes: list = []
    page = 0
    while True:
        url = f"{TVDB_BASE_URL}/series/{tvdb_id}/episodes/default?page={page}"
        resp = _make_request(url)
        if not resp or resp.status_code != 200:
            break

        data = resp.json().get('data', {})
        episodes = data.get('episodes', [])
        if not episodes:
            break

        all_episodes.extend(episodes)

        # Check for next page
        links = resp.json().get('links', {})
        if links.get('next'):
            page += 1
        else:
            break

    if not all_episodes:
        return None

    # Group into seasons
    eps_by_season: defaultdict = defaultdict(list)
    for ep in all_episodes:
        if isinstance(ep, dict):
            sn = ep.get('seasonNumber', 0)
            eps_by_season[sn].append(ep)

    processed: dict = {}
    for sn, episodes in eps_by_season.items():
        ep_dict: dict = {}
        for ep in episodes:
            ep_num = ep.get('number')
            if ep_num is None:
                continue
            ep_dict[ep_num] = {
                'title': ep.get('name', ''),
                'overview': ep.get('overview', ''),
                'runtime': ep.get('runtime', 0),
                'first_aired': _format_air_date(ep.get('aired'), airs_time, airs_timezone),
                'imdb_id': None,
                'absolute': ep.get('absoluteNumber'),
            }
        processed[sn] = {
            'episode_count': len(ep_dict),
            'episodes': ep_dict,
        }

    return processed if processed else None


def get_show_aliases(imdb_id: str) -> Optional[dict]:
    """Get show aliases. Takes imdb_id (not slug) unlike trakt_client."""
    tvdb_id = _resolve_tvdb_id(imdb_id, media_type='show')
    if not tvdb_id:
        return None

    url = f"{TVDB_BASE_URL}/series/{tvdb_id}/extended?meta=translations"
    resp = _make_request(url)
    if not resp or resp.status_code != 200:
        return None

    raw = resp.json().get('data', {})
    translations = raw.get('translations', {}).get('nameTranslations', [])
    aliases = _extract_aliases(translations)
    return aliases if aliases else None


def get_movie_data(imdb_id: str) -> Optional[dict]:
    """Get full movie metadata + aliases + release dates."""
    tvdb_id = _resolve_tvdb_id(imdb_id, media_type='movie')
    if not tvdb_id:
        logger.warning(f"TVDB: could not resolve IMDb {imdb_id} to TVDB movie ID, trying TMDB fallback")
        tmdb_api_key = _get_tmdb_api_key()
        if tmdb_api_key:
            return _fetch_tmdb_movie_data(imdb_id, tmdb_api_key)
        return None

    url = f"{TVDB_BASE_URL}/movies/{tvdb_id}/extended?meta=translations"
    resp = _make_request(url)
    if not resp or resp.status_code != 200:
        return None

    raw = resp.json().get('data', {})
    if not raw:
        return None

    data = _build_movie_dict(raw, imdb_id, tvdb_id)

    # Aliases from translations
    translations = raw.get('translations', {}).get('nameTranslations', [])
    aliases = _extract_aliases(translations)
    if aliases:
        data['aliases'] = aliases

    # Release dates
    releases = _extract_movie_releases(raw)
    if releases:
        data['release_dates'] = releases

    return data


def _build_movie_dict(raw: dict, imdb_id: str, tvdb_id: int) -> dict:
    """Build a Trakt-compatible movie metadata dict from TVDB movie data."""
    ids = {
        'imdb': imdb_id,
        'tvdb': tvdb_id,
        'slug': raw.get('slug', ''),
    }

    # Extract TMDB ID from remoteIds
    remote_ids = raw.get('remoteIds', []) or raw.get('remote_ids', [])
    if isinstance(remote_ids, list):
        for rid in remote_ids:
            if isinstance(rid, dict):
                source = (rid.get('sourceName', '') or '').lower()
                if 'tmdb' in source or 'themoviedb' in source:
                    try:
                        ids['tmdb'] = int(rid.get('id', ''))
                    except (ValueError, TypeError):
                        pass
                    break

    # Extract year
    year = None
    if raw.get('year'):
        try:
            year = int(raw['year'])
        except (ValueError, TypeError):
            pass
    if not year:
        first_release = raw.get('first_release', {})
        date_str = first_release.get('date', '') if isinstance(first_release, dict) else ''
        if date_str and len(date_str) >= 4:
            try:
                year = int(date_str[:4])
            except ValueError:
                pass

    # Find English overview/title
    title = raw.get('name', '')
    overview = raw.get('overview', '')
    for t in raw.get('translations', {}).get('nameTranslations', []):
        if t.get('language') == 'eng' and t.get('name'):
            title = t['name']
            break
    for t in raw.get('translations', {}).get('overviewTranslations', []):
        if t.get('language') == 'eng':
            overview = t.get('overview', overview)
            break

    return {
        'title': title,
        'year': year,
        'ids': ids,
        'overview': overview,
        'runtime': raw.get('runtime'),
        'status': _map_status(raw.get('status', {}).get('name') if isinstance(raw.get('status'), dict) else raw.get('status')),
        'genres': [g.get('name', '') for g in raw.get('genres', []) if isinstance(g, dict)],
        'type': 'movie',
    }


def _extract_movie_releases(raw: dict) -> Optional[dict]:
    """Extract release dates from TVDB movie data.

    TVDB releases only have country, date, and detail fields — no type field.
    We default to 'theatrical' since TVDB primarily tracks theatrical dates.
    """
    releases_raw = raw.get('releases', [])
    if not releases_raw:
        return None

    formatted: defaultdict = defaultdict(list)
    for release in releases_raw:
        if not isinstance(release, dict):
            continue
        country = release.get('country')
        date_str = release.get('date')
        # TVDB has no 'type' field — use 'detail' as hint, default to 'theatrical'
        # 'global' country entries are typically premiere dates
        detail = (release.get('detail') or '').lower()
        if 'physical' in detail:
            release_type = 'physical'
        elif 'digital' in detail:
            release_type = 'digital'
        elif 'premiere' in detail or country == 'global':
            release_type = 'premiere'
        elif 'tv' in detail:
            release_type = 'tv'
        else:
            release_type = 'theatrical'
        if country and date_str:
            # Normalize date to YYYY-MM-DD
            date_part = date_str[:10] if len(date_str) >= 10 else date_str
            try:
                dt_obj = datetime.fromisoformat(date_part)
                try:
                    from metadata.metadata import _get_local_timezone
                    dt_obj = dt_obj.astimezone(_get_local_timezone())
                except ImportError:
                    pass
                formatted[country].append({
                    'date': dt_obj.date().isoformat() if hasattr(dt_obj, 'date') else date_part,
                    'type': release_type,
                })
            except Exception:
                formatted[country].append({
                    'date': date_part,
                    'type': release_type,
                })

    return dict(formatted) if formatted else None


def get_movie_aliases(imdb_id: str) -> Optional[dict]:
    """Get movie aliases. Takes imdb_id (not slug) unlike trakt_client."""
    tvdb_id = _resolve_tvdb_id(imdb_id, media_type='movie')
    if not tvdb_id:
        return None

    url = f"{TVDB_BASE_URL}/movies/{tvdb_id}/extended?meta=translations"
    resp = _make_request(url)
    if not resp or resp.status_code != 200:
        return None

    raw = resp.json().get('data', {})
    translations = raw.get('translations', {}).get('nameTranslations', [])
    aliases = _extract_aliases(translations)
    return aliases if aliases else None


# TMDB release type integer → string mapping
_TMDB_RELEASE_TYPES = {
    1: 'premiere',
    2: 'theatrical (limited)',
    3: 'theatrical',
    4: 'digital',
    5: 'physical',
    6: 'tv',
}


def _fetch_tmdb_release_dates(tmdb_id: int, api_key: str) -> Optional[dict]:
    """Fetch typed release dates from TMDB for a movie.

    Returns Trakt-compatible dict: {'us': [{'date': 'YYYY-MM-DD', 'type': 'theatrical'}, ...], ...}
    """
    try:
        resp = requests.get(
            f"https://api.themoviedb.org/3/movie/{tmdb_id}/release_dates",
            params={'api_key': api_key},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.debug(f"TMDB release dates request failed for tmdb_id={tmdb_id}: HTTP {resp.status_code}")
            return None

        data = resp.json()
        results = data.get('results', [])
        if not results:
            return None

        formatted: defaultdict = defaultdict(list)
        for entry in results:
            country = (entry.get('iso_3166_1') or '').lower()
            if not country:
                continue
            for rd in entry.get('release_dates', []):
                type_int = rd.get('type')
                release_type = _TMDB_RELEASE_TYPES.get(type_int)
                date_str = rd.get('release_date', '')
                if release_type and date_str:
                    date_part = date_str[:10]
                    formatted[country].append({
                        'date': date_part,
                        'type': release_type,
                    })

        return dict(formatted) if formatted else None
    except Exception as e:
        logger.debug(f"TMDB release dates error for tmdb_id={tmdb_id}: {e}")
        return None


def _resolve_tmdb_id_from_imdb(imdb_id: str, api_key: str, media_type: str = 'movie') -> Optional[int]:
    """Resolve an IMDb ID to a TMDB ID via TMDB's /find endpoint."""
    try:
        resp = requests.get(
            f"https://api.themoviedb.org/3/find/{imdb_id}",
            params={'api_key': api_key, 'external_source': 'imdb_id'},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return None

        data = resp.json()
        if media_type == 'show':
            tv_results = data.get('tv_results', [])
            if tv_results:
                return tv_results[0].get('id')
        else:
            movie_results = data.get('movie_results', [])
            if movie_results:
                return movie_results[0].get('id')
        return None
    except Exception as e:
        logger.debug(f"TMDB find by IMDb error for {imdb_id}: {e}")
        return None


def _get_tmdb_api_key() -> str:
    """Return the configured TMDB API key, or empty string."""
    try:
        from utilities.settings import get_setting
        return get_setting('TMDB', 'api_key', default='') or ''
    except Exception:
        return ''


def _fetch_tmdb_movie_data(imdb_id: str, api_key: str) -> Optional[dict]:
    """Fetch movie metadata from TMDB as fallback when TVDB cannot resolve the ID."""
    tmdb_id = _resolve_tmdb_id_from_imdb(imdb_id, api_key, media_type='movie')
    if not tmdb_id:
        return None

    try:
        resp = requests.get(
            f"https://api.themoviedb.org/3/movie/{tmdb_id}",
            params={'api_key': api_key},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return None

        raw = resp.json()
        year = None
        release_date = raw.get('release_date', '')
        if release_date and len(release_date) >= 4:
            try:
                year = int(release_date[:4])
            except ValueError:
                pass

        status_raw = (raw.get('status') or '').lower()
        status = 'released' if status_raw == 'released' else status_raw

        genres = [g.get('name', '') for g in raw.get('genres', []) if isinstance(g, dict)]

        data = {
            'title': raw.get('title', ''),
            'year': year,
            'ids': {'imdb': imdb_id, 'tmdb': tmdb_id},
            'overview': raw.get('overview', ''),
            'runtime': raw.get('runtime'),
            'status': status,
            'genres': genres,
            'type': 'movie',
        }

        logger.info(f"TMDB fallback: got movie metadata for {imdb_id} (tmdb_id={tmdb_id})")
        return data
    except Exception as e:
        logger.debug(f"TMDB movie data error for {imdb_id}: {e}")
        return None


def _fetch_tmdb_show_data(imdb_id: str, api_key: str) -> Optional[dict]:
    """Fetch show metadata from TMDB as fallback when TVDB cannot resolve the ID."""
    tmdb_id = _resolve_tmdb_id_from_imdb(imdb_id, api_key, media_type='show')
    if not tmdb_id:
        return None

    try:
        resp = requests.get(
            f"https://api.themoviedb.org/3/tv/{tmdb_id}",
            params={'api_key': api_key},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return None

        raw = resp.json()
        year = None
        first_air = raw.get('first_air_date', '')
        if first_air and len(first_air) >= 4:
            try:
                year = int(first_air[:4])
            except ValueError:
                pass

        status_raw = (raw.get('status') or '').lower()
        status_map = {
            'returning series': 'returning series',
            'ended': 'ended',
            'canceled': 'canceled',
            'in production': 'in production',
        }
        status = status_map.get(status_raw, status_raw)

        genres = [g.get('name', '') for g in raw.get('genres', []) if isinstance(g, dict)]

        # Build seasons dict
        seasons = {}
        for s in raw.get('seasons', []):
            sn = s.get('season_number')
            if sn is not None:
                seasons[str(sn)] = {
                    'number': sn,
                    'episode_count': s.get('episode_count', 0),
                    'episodes': {},
                }

        data = {
            'title': raw.get('name', ''),
            'year': year,
            'ids': {'imdb': imdb_id, 'tmdb': tmdb_id},
            'overview': raw.get('overview', ''),
            'runtime': (raw.get('episode_run_time') or [None])[0],
            'status': status,
            'genres': genres,
            'type': 'show',
            'seasons': seasons,
        }

        logger.info(f"TMDB fallback: got show metadata for {imdb_id} (tmdb_id={tmdb_id})")
        return data
    except Exception as e:
        logger.debug(f"TMDB show data error for {imdb_id}: {e}")
        return None


def get_movie_release_dates(imdb_id: str) -> Optional[dict]:
    """Fetch release dates for a movie.

    Fetches TVDB releases first, then supplements with TMDB typed release dates
    (digital, physical, etc.) when a TMDB API key is configured.
    """
    tvdb_id = _resolve_tvdb_id(imdb_id, media_type='movie')
    tmdb_api_key = _get_tmdb_api_key()

    if not tvdb_id:
        # TVDB can't resolve — try TMDB-only release dates
        if tmdb_api_key:
            tmdb_id = _resolve_tmdb_id_from_imdb(imdb_id, tmdb_api_key, media_type='movie')
            if tmdb_id:
                releases = _fetch_tmdb_release_dates(tmdb_id, tmdb_api_key)
                if releases:
                    logger.info(f"TMDB fallback: got release dates for {imdb_id} (tmdb_id={tmdb_id})")
                    return releases
        return None

    url = f"{TVDB_BASE_URL}/movies/{tvdb_id}/extended"
    resp = _make_request(url)
    if not resp or resp.status_code != 200:
        return None

    raw = resp.json().get('data', {})
    releases = _extract_movie_releases(raw) or {}

    if tmdb_api_key:
        # Try to get TMDB ID from TVDB remoteIds first
        tmdb_id = None
        remote_ids = raw.get('remoteIds', []) or raw.get('remote_ids', [])
        if isinstance(remote_ids, list):
            for rid in remote_ids:
                if isinstance(rid, dict):
                    source = (rid.get('sourceName', '') or '').lower()
                    if 'tmdb' in source or 'themoviedb' in source:
                        try:
                            tmdb_id = int(rid.get('id', ''))
                        except (ValueError, TypeError):
                            pass
                        break

        # Fallback: resolve via TMDB find endpoint
        if not tmdb_id:
            tmdb_id = _resolve_tmdb_id_from_imdb(imdb_id, tmdb_api_key)

        if tmdb_id:
            tmdb_releases = _fetch_tmdb_release_dates(tmdb_id, tmdb_api_key)
            logger.debug(f"TMDB supplement for {imdb_id} (tmdb_id={tmdb_id}): {tmdb_releases}")
            if tmdb_releases:
                # Merge: for each country, add TMDB releases for types not already present
                for country, tmdb_entries in tmdb_releases.items():
                    existing_types = {
                        (r.get('type') or '').lower()
                        for r in releases.get(country, [])
                    }
                    for entry in tmdb_entries:
                        if (entry.get('type') or '').lower() not in existing_types:
                            releases.setdefault(country, []).append(entry)
        else:
            logger.debug(f"TMDB supplement for {imdb_id}: could not resolve TMDB ID")

    return releases if releases else None


def get_updated_shows(since_iso: str) -> List[dict]:
    """Get shows updated since the given ISO timestamp.

    TVDB /updates returns TVDB IDs; we can only resolve items already
    in our TVDBToIMDBMapping cache.
    """
    since_unix = _iso_to_unix(since_iso)
    if since_unix is None:
        return []

    url = f"{TVDB_BASE_URL}/updates?since={since_unix}&type=series&action=update"
    resp = _make_request(url)
    if not resp or resp.status_code != 200:
        return []

    data = resp.json().get('data', [])
    if not data:
        return []

    # Collect TVDB IDs from updates
    tvdb_ids = set()
    timestamps = {}
    for item in data:
        if isinstance(item, dict):
            record_id = item.get('recordId') or item.get('entityId')
            if record_id:
                tvdb_ids.add(str(record_id))
                ts = item.get('timeStamp')
                if ts:
                    timestamps[str(record_id)] = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

    if not tvdb_ids:
        return []

    # Cross-reference with our cached mappings
    results: list = []
    with managed_session() as session:
        mappings = session.query(TVDBToIMDBMapping).filter(
            TVDBToIMDBMapping.tvdb_id.in_(tvdb_ids),
        ).all()
        for m in mappings:
            results.append({
                'imdb_id': m.imdb_id,
                'updated_at': timestamps.get(m.tvdb_id, since_iso),
            })

    logger.info(f"TVDB: fetched {len(data)} series updates, {len(results)} matched our cache")
    return results


def get_updated_movies(since_iso: str) -> List[dict]:
    """Get movies updated since the given ISO timestamp."""
    since_unix = _iso_to_unix(since_iso)
    if since_unix is None:
        return []

    url = f"{TVDB_BASE_URL}/updates?since={since_unix}&type=movies&action=update"
    resp = _make_request(url)
    if not resp or resp.status_code != 200:
        return []

    data = resp.json().get('data', [])
    if not data:
        return []

    tvdb_ids = set()
    timestamps = {}
    for item in data:
        if isinstance(item, dict):
            record_id = item.get('recordId') or item.get('entityId')
            if record_id:
                tvdb_ids.add(str(record_id))
                ts = item.get('timeStamp')
                if ts:
                    timestamps[str(record_id)] = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

    if not tvdb_ids:
        return []

    results: list = []
    with managed_session() as session:
        mappings = session.query(TVDBToIMDBMapping).filter(
            TVDBToIMDBMapping.tvdb_id.in_(tvdb_ids),
        ).all()
        for m in mappings:
            results.append({
                'imdb_id': m.imdb_id,
                'updated_at': timestamps.get(m.tvdb_id, since_iso),
            })

    logger.info(f"TVDB: fetched {len(data)} movie updates, {len(results)} matched our cache")
    return results


def search_by_imdb(imdb_id: str) -> Optional[dict]:
    """Search by IMDb ID. Returns a Trakt-style wrapper dict."""
    tvdb_id = _resolve_tvdb_id(imdb_id)
    if not tvdb_id:
        return None

    # Try as series first
    url = f"{TVDB_BASE_URL}/series/{tvdb_id}"
    resp = _make_request(url)
    if resp and resp.status_code == 200:
        raw = resp.json().get('data', {})
        if raw:
            return {
                'type': 'show',
                'show': {
                    'ids': {'imdb': imdb_id, 'tvdb': tvdb_id, 'slug': raw.get('slug', '')},
                    'title': raw.get('name', ''),
                }
            }

    # Try as movie
    url = f"{TVDB_BASE_URL}/movies/{tvdb_id}"
    resp = _make_request(url)
    if resp and resp.status_code == 200:
        raw = resp.json().get('data', {})
        if raw:
            return {
                'type': 'movie',
                'movie': {
                    'ids': {'imdb': imdb_id, 'tvdb': tvdb_id, 'slug': raw.get('slug', '')},
                    'title': raw.get('name', ''),
                }
            }

    return None


def convert_tmdb_to_imdb(tmdb_id: str, media_type: str | None = None) -> Tuple[Optional[str], Optional[str]]:
    """Convert TMDB ID to IMDb ID via TVDB search."""
    url = f"{TVDB_BASE_URL}/search/remoteid/{tmdb_id}"
    resp = _make_request(url)
    if not resp or resp.status_code != 200:
        return None, None

    data = resp.json().get('data', [])
    if not data:
        return None, None

    for item in data:
        # Look for IMDb ID in remote_ids
        remote_ids = item.get('remote_ids', []) or item.get('remoteIds', [])
        if isinstance(remote_ids, list):
            for rid in remote_ids:
                if isinstance(rid, dict):
                    rid_val = rid.get('id', '')
                    if rid_val and rid_val.startswith('tt'):
                        return rid_val, 'tvdb'

        # Also check the item's own IMDb references
        imdb_id = item.get('imdb_id')
        if imdb_id:
            return imdb_id, 'tvdb'

    # Fallback: resolve the TVDB ID and look up the mapping
    for item in data:
        tvdb_id = item.get('id')
        item_type = item.get('type', '').lower()
        if not tvdb_id:
            continue

        # Try to get extended data which may include remoteIds
        if item_type in ('series', 'show'):
            ext_url = f"{TVDB_BASE_URL}/series/{tvdb_id}/extended"
        elif item_type == 'movie':
            ext_url = f"{TVDB_BASE_URL}/movies/{tvdb_id}/extended"
        else:
            continue

        ext_resp = _make_request(ext_url)
        if ext_resp and ext_resp.status_code == 200:
            ext_data = ext_resp.json().get('data', {})
            remote_ids = ext_data.get('remoteIds', [])
            if isinstance(remote_ids, list):
                for rid in remote_ids:
                    if isinstance(rid, dict):
                        source_name = (rid.get('sourceName', '') or '').lower()
                        rid_val = rid.get('id', '')
                        if 'imdb' in source_name and rid_val and rid_val.startswith('tt'):
                            return rid_val, 'tvdb'

    return None, None


def search_media(query: str, year: Optional[int] = None,
                 media_type: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    """Search TVDB for media. Returns Trakt-compatible result list."""
    params = {'query': query}
    if media_type:
        if media_type == 'movie':
            params['type'] = 'movie'
        elif media_type in ('show', 'series'):
            params['type'] = 'series'
    if year:
        params['year'] = str(year)

    url = f"{TVDB_BASE_URL}/search?{urlencode(params)}"
    resp = _make_request(url)
    if not resp or resp.status_code != 200:
        return None

    data = resp.json().get('data', [])
    if not data:
        return None

    results: list = []
    for item in data:
        if not isinstance(item, dict):
            continue

        item_type = item.get('type', '').lower()
        if item_type in ('series', 'show'):
            result_type = 'show'
        elif item_type == 'movie':
            result_type = 'movie'
        else:
            continue

        title = item.get('name', '') or item.get('translated_name', '')
        item_year = None
        if item.get('year'):
            try:
                item_year = int(item['year'])
            except (ValueError, TypeError):
                pass
        elif item.get('first_air_time', ''):
            try:
                item_year = int(item['first_air_time'][:4])
            except (ValueError, TypeError, IndexError):
                pass

        # Year filter
        if year and item_year and item_year != year:
            continue

        # Extract IMDb ID from remote_ids if available
        imdb_id = None
        tmdb_id = None
        remote_ids = item.get('remote_ids', []) or item.get('remoteIds', [])
        if isinstance(remote_ids, list):
            for rid in remote_ids:
                if isinstance(rid, dict):
                    rid_val = rid.get('id', '')
                    source = (rid.get('sourceName', '') or '').lower()
                    if 'imdb' in source and rid_val.startswith('tt'):
                        imdb_id = rid_val
                    elif 'tmdb' in source or 'themoviedb' in source:
                        try:
                            tmdb_id = int(rid_val)
                        except (ValueError, TypeError):
                            pass

        tvdb_id_str = item.get('tvdb_id') or item.get('id')

        results.append({
            'title': title,
            'year': item_year,
            'imdb_id': imdb_id,
            'tmdb_id': tmdb_id,
            'type': result_type,
        })

    return results if results else None


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _iso_to_unix(iso_str: str) -> Optional[int]:
    """Convert ISO 8601 datetime string to Unix timestamp."""
    try:
        dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception as e:
        logger.warning(f"Could not parse ISO timestamp '{iso_str}': {e}")
        return None
