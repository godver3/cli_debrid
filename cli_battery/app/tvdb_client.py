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

# Network name → IANA timezone mapping for international networks
# Used when TVDB doesn't provide timezone in originalNetwork.timezone
_NETWORK_TIMEZONE_MAP = {
    # South Korean networks
    'KBS': 'Asia/Seoul',
    'KBS 2': 'Asia/Seoul',
    'KBS1': 'Asia/Seoul',
    'KBS2': 'Asia/Seoul',
    'MBC': 'Asia/Seoul',
    'SBS': 'Asia/Seoul',
    'tvN': 'Asia/Seoul',
    'JTBC': 'Asia/Seoul',
    'OCN': 'Asia/Seoul',
    # Japanese networks
    'NHK': 'Asia/Tokyo',
    'Fuji TV': 'Asia/Tokyo',
    'TBS': 'Asia/Tokyo',
    'TV Tokyo': 'Asia/Tokyo',
    'TV Asahi': 'Asia/Tokyo',
    'Nippon TV': 'Asia/Tokyo',
    # UK networks
    'BBC One': 'Europe/London',
    'BBC Two': 'Europe/London',
    'ITV': 'Europe/London',
    'Channel 4': 'Europe/London',
    # Add more as needed
}

# Country code → default IANA timezone fallback
# TVDB airsTime is in the show's local broadcast timezone
_COUNTRY_TIMEZONE_MAP = {
    'usa': 'America/New_York',
    'can': 'America/New_York',
    'gbr': 'Europe/London',
    'aus': 'Australia/Sydney',
    'jpn': 'Asia/Tokyo',
    'kor': 'Asia/Seoul',
    'deu': 'Europe/Berlin',
    'fra': 'Europe/Paris',
    'ita': 'Europe/Rome',
    'esp': 'Europe/Madrid',
    'bra': 'America/Sao_Paulo',
    'ind': 'Asia/Kolkata',
    'nzl': 'Pacific/Auckland',
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
        query = session.query(TVDBToIMDBMapping).filter_by(imdb_id=imdb_id)
        if media_type:
            query = query.filter_by(media_type=media_type)
        mapping = query.first()
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

    # Last resort: take the first result with any ID (only when no type filter)
    if not media_type:
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


def get_season_poster_url(imdb_id: str, season_number: int) -> Optional[str]:
    """Return the best English season poster URL from TVDB for a TV show season.

    Requires TVDB API key in settings. Returns None if unavailable.
    Filters for type=7 (season poster) and language='eng', ignores score.
    """
    if not is_available():
        return None
    try:
        tvdb_series_id = _resolve_tvdb_id(imdb_id, 'show')
        if not tvdb_series_id:
            logger.debug(f"[TVDB] Could not resolve series ID for {imdb_id}")
            return None

        # Get season list to find the TVDB season ID for the target season
        # Use Aired Order (type.id=1) to match standard season numbering
        series_resp = _make_request(f"{TVDB_BASE_URL}/series/{tvdb_series_id}/extended?meta=episodes,translations")
        if not series_resp or series_resp.status_code != 200:
            return None

        seasons = series_resp.json().get('data', {}).get('seasons', [])
        tvdb_season_id = None
        for s in seasons:
            if s.get('number') == season_number and s.get('type', {}).get('id') == 1:
                tvdb_season_id = s.get('id')
                break
        # Fallback: any season with matching number if no Aired Order found
        if tvdb_season_id is None:
            for s in seasons:
                if s.get('number') == season_number:
                    tvdb_season_id = s.get('id')
                    break

        if not tvdb_season_id:
            logger.debug(f"[TVDB] No season {season_number} found for series {tvdb_series_id}")
            return None

        # Fetch season extended to get artwork
        season_resp = _make_request(f"{TVDB_BASE_URL}/seasons/{tvdb_season_id}/extended")
        if not season_resp or season_resp.status_code != 200:
            return None

        artwork = season_resp.json().get('data', {}).get('artwork', [])
        # Filter: type 7 = season poster, language = eng
        eng_posters = [a for a in artwork if a.get('type') == 7 and a.get('language') == 'eng']
        if not eng_posters:
            logger.debug(f"[TVDB] No English season posters for series {tvdb_series_id} season {season_number}")
            return None

        # Sort by score descending, take first
        eng_posters.sort(key=lambda a: a.get('score', 0), reverse=True)
        poster_url = eng_posters[0].get('image')
        logger.info(f"[TVDB] Found English season poster for {imdb_id} S{season_number:02d}: {poster_url}")
        return poster_url

    except Exception as e:
        logger.debug(f"[TVDB] get_season_poster_url error for {imdb_id} S{season_number}: {e}")
        return None


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


def _get_trakt_episode_air_date(imdb_id: str, season: int, episode: int) -> str | None:
    """Fetch episode first_aired from Trakt as fallback when TVDB has incomplete data."""
    try:
        from . import trakt_client
        seasons_data, source = trakt_client.get_show_seasons_and_episodes(imdb_id, include_specials=True)
        if seasons_data and season in seasons_data:
            season_data = seasons_data[season]
            if 'episodes' in season_data and episode in season_data['episodes']:
                ep_data = season_data['episodes'][episode]
                return ep_data.get('first_aired')
    except Exception as e:
        logger.debug(f"Could not fetch Trakt episode data for {imdb_id} S{season:02d}E{episode:02d}: {e}")
    return None


def _batch_fetch_episode_air_dates(imdb_id: str, tmdb_id: int = None) -> dict:
    """Batch-fetch all episode air dates from TMDB first (fast), then Trakt as fallback.

    Returns a dict: {(season, episode): 'YYYY-MM-DDTHH:MM:SS.000Z', ...}
    This avoids making 100+ individual API calls for each episode.

    Flow: TVDB → TMDB (batch) → Trakt (batch with 30s timeout) → midnight UTC fallback
    """
    air_dates = {}

    # Try TMDB first (faster, no rate limits)
    if tmdb_id:
        try:
            from utilities.settings import get_setting
            tmdb_api_key = get_setting('TMDB', 'api_key', '')
            if tmdb_api_key:
                logger.info(f"Batch-fetching episode air dates from TMDB for {imdb_id} (TMDB ID: {tmdb_id})")
                url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={tmdb_api_key}&append_to_response=external_ids"
                resp = requests.get(url, timeout=15)
                if resp.status_code == 200:
                    data = resp.json()
                    seasons = data.get('seasons', [])

                    # Fetch each season's episodes
                    for season_info in seasons:
                        season_num = season_info.get('season_number')
                        if season_num is None:
                            continue

                        season_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_num}?api_key={tmdb_api_key}"
                        season_resp = requests.get(season_url, timeout=15)
                        if season_resp.status_code == 200:
                            season_data = season_resp.json()
                            episodes = season_data.get('episodes', [])
                            for ep in episodes:
                                ep_num = ep.get('episode_number')
                                air_date = ep.get('air_date')
                                # TMDB returns date + time in 'air_date' field
                                if air_date and ep_num is not None:
                                    # Convert TMDB format to ISO 8601 UTC
                                    if 'T' not in air_date:
                                        air_date = f"{air_date}T00:00:00.000Z"
                                    air_dates[(season_num, ep_num)] = air_date

                    if air_dates:
                        logger.info(f"✅ TMDB: Fetched {len(air_dates)} episode air dates for {imdb_id}")
                        return air_dates
        except Exception as e:
            logger.debug(f"TMDB batch fetch failed for {imdb_id}: {e}")

    # Fallback to Trakt (all episodes in one call) - with increased timeout
    try:
        from . import trakt_client
        logger.info(f"Batch-fetching episode air dates from Trakt for {imdb_id}")

        # Temporarily increase timeout for batch operations (10s is too short for shows with many episodes)
        old_timeout = trakt_client.REQUEST_TIMEOUT
        try:
            trakt_client.REQUEST_TIMEOUT = 30  # Increase from 10s to 30s for batch fetch
            seasons_data, source = trakt_client.get_show_seasons_and_episodes(imdb_id, include_specials=True)
            if seasons_data:
                for season_num, season_data in seasons_data.items():
                    if 'episodes' in season_data:
                        for ep_num, ep_data in season_data['episodes'].items():
                            first_aired = ep_data.get('first_aired')
                            if first_aired:
                                air_dates[(season_num, ep_num)] = first_aired

                if air_dates:
                    logger.info(f"✅ Trakt: Fetched {len(air_dates)} episode air dates for {imdb_id}")
                    return air_dates
        finally:
            trakt_client.REQUEST_TIMEOUT = old_timeout  # Always restore original timeout

    except Exception as e:
        logger.warning(f"Trakt batch fetch failed for {imdb_id}: {e}")

    return air_dates


def _format_air_date(date_str: str | None, airs_time: str | None = None,
                     airs_timezone: str | None = None, imdb_id: str = None,
                     season: int = None, episode: int = None,
                     cached_air_dates: dict = None) -> str | None:
    """Convert TVDB 'YYYY-MM-DD' to ISO 8601 UTC datetime.

    TVDB episode dates are in the show's local timezone (date-only).  When
    *airs_time* (HH:MM) and *airs_timezone* (IANA tz name) are provided we
    combine them with the date to produce a proper UTC timestamp, matching what
    Trakt returns.

    If TVDB only provides date without time, and no timezone is available,
    looks up cached air date from TMDB/Trakt batch fetch to avoid individual API calls.
    """
    if not date_str:
        return None
    # Already ISO 8601 format with time component
    if 'T' in date_str:
        return date_str

    # If no timezone available, check cached air dates first (from batch fetch)
    if not airs_timezone and cached_air_dates and season is not None and episode is not None:
        cached_date = cached_air_dates.get((season, episode))
        if cached_date:
            return cached_date

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


def _infer_timezone_from_network(network_name: str | None, country: str | None = None) -> str | None:
    """Infer IANA timezone from network name or country when TVDB doesn't provide it."""
    if network_name:
        # Direct match
        if network_name in _NETWORK_TIMEZONE_MAP:
            return _NETWORK_TIMEZONE_MAP[network_name]

        # Partial match (case-insensitive)
        network_lower = network_name.lower()
        for net_key, tz in _NETWORK_TIMEZONE_MAP.items():
            if net_key.lower() in network_lower:
                return tz

    # Fall back to country code
    if country:
        return _COUNTRY_TIMEZONE_MAP.get(country.lower())

    return None


# ─── Public API (matches trakt_client signatures) ────────────────────────────


def _resolve_tvdb_id_via_tmdb(imdb_id: str, tmdb_api_key: str) -> Optional[int]:
    """Try to resolve a TVDB series ID by looking up via TMDB ID.

    Used when /search/remoteid/{imdb_id} returns null (TVDB hasn't linked the
    IMDb ID yet) but the show exists on TVDB and can be found via its TMDB ID.
    Returns the TVDB series ID if found, else None.
    """
    tmdb_id = _resolve_tmdb_id_from_imdb(imdb_id, tmdb_api_key, media_type='show')
    if not tmdb_id:
        return None

    url = f"{TVDB_BASE_URL}/search/remoteid/{tmdb_id}"
    resp = _make_request(url)
    if not resp or resp.status_code != 200:
        return None

    data = resp.json().get('data') or []
    for item in data:
        series = item.get('series') if isinstance(item, dict) else None
        if isinstance(series, dict) and series.get('id'):
            tvdb_id = int(series['id'])
            # Cache the mapping so future lookups don't need the TMDB roundtrip
            _cache_tvdb_mapping(str(tvdb_id), imdb_id, 'show')
            logger.info(f"TVDB: resolved {imdb_id} via TMDB ID {tmdb_id} → TVDB ID {tvdb_id}")
            return tvdb_id

    return None


def _get_trakt_status(imdb_id: str) -> Optional[str]:
    """Lightweight Trakt status check — fetches only show summary, returns status string or None.

    Trakt is optional enrichment here. Do not initialize or refresh legacy OAuth
    state when the current Trakt Client ID is blank.

    Skips immediately if the GlobalTraktCoordinator reports an active cooldown —
    avoids blocking the caller (e.g. a web request) for the full cooldown period.
    """
    try:
        from utilities.settings import get_setting
        if not str(get_setting('Trakt', 'client_id', '') or '').strip():
            logger.debug(f"TVDB: Trakt not configured; skipping status cross-check for {imdb_id}")
            return None
    except Exception:
        return None

    try:
        from utilities.trakt_coordinator import GlobalTraktCoordinator
        cooldown = GlobalTraktCoordinator.get_instance().get_cooldown_status()
        if cooldown.get('active'):
            logger.debug(
                f"TVDB: skipping Trakt status cross-check for {imdb_id} "
                f"— Trakt cooldown active ({cooldown.get('remaining_seconds', 0):.0f}s remaining)"
            )
            return None
    except Exception:
        pass

    try:
        from . import trakt_client
        from .trakt_client import TRAKT_BASE_URL, _make_request as trakt_request
        resp = trakt_request(f"{TRAKT_BASE_URL}/shows/{imdb_id}?extended=full")
        if resp and resp.status_code == 200:
            return resp.json().get('status')
    except Exception as e:
        logger.debug(f"TVDB: Trakt status cross-check failed for {imdb_id}: {e}")
    return None


def get_show_data(imdb_id: str) -> Optional[dict]:
    """Get full show metadata + aliases + seasons/episodes."""
    # No usable TVDB key: serve the whole show from TMDB rather than resolving a
    # TVDB id we cannot then fetch with. Resolving one via TMDB and continuing
    # would send us into the TVDB request below, whose failure path falls back to
    # Trakt - useless on a setup that has neither.
    if not is_available():
        tmdb_api_key = _get_tmdb_api_key()
        if tmdb_api_key:
            return _fetch_tmdb_show_data(imdb_id, tmdb_api_key)
        return None

    tvdb_id = _resolve_tvdb_id(imdb_id, media_type='show')
    if not tvdb_id:
        # TVDB hasn't linked this IMDb ID yet — try resolving via TMDB ID
        tmdb_api_key = _get_tmdb_api_key()
        if tmdb_api_key:
            tvdb_id = _resolve_tvdb_id_via_tmdb(imdb_id, tmdb_api_key)
        if not tvdb_id:
            logger.warning(f"TVDB: could not resolve IMDb {imdb_id} to TVDB ID, trying TMDB fallback")
            if tmdb_api_key:
                return _fetch_tmdb_show_data(imdb_id, tmdb_api_key)
            return None

    url = f"{TVDB_BASE_URL}/series/{tvdb_id}/extended?meta=translations,episodes"
    resp = _make_request(url)
    if not resp or resp.status_code != 200:
        logger.warning(f"TVDB metadata fetch failed for show {imdb_id} (TVDB ID: {tvdb_id}), trying Trakt fallback")
        from . import trakt_client
        return trakt_client.get_show_data(imdb_id)

    raw = resp.json().get('data', {})
    if not raw:
        logger.warning(f"TVDB returned empty data for show {imdb_id} (TVDB ID: {tvdb_id}), trying Trakt fallback")
        from . import trakt_client
        return trakt_client.get_show_data(imdb_id)

    # Validate that the TVDB show's IMDb ID matches what we requested.
    # A stale cache entry could map the wrong TVDB ID to this IMDb ID.
    remote_ids_raw = raw.get('remoteIds', [])
    tvdb_imdb_id = None
    if isinstance(remote_ids_raw, list):
        for rid in remote_ids_raw:
            if isinstance(rid, dict):
                source_name = (rid.get('sourceName', '') or '').lower()
                rid_val = rid.get('id', '')
                if 'imdb' in source_name and rid_val and rid_val.startswith('tt'):
                    tvdb_imdb_id = rid_val
                    break
    if tvdb_imdb_id and tvdb_imdb_id != imdb_id:
        logger.warning(
            f"TVDB IMDb mismatch for show: requested {imdb_id}, TVDB series {tvdb_id} has {tvdb_imdb_id}. "
            f"Invalidating stale cache entry and falling back to Trakt."
        )
        try:
            with managed_session() as session:
                stale = session.query(TVDBToIMDBMapping).filter_by(imdb_id=imdb_id, tvdb_id=str(tvdb_id)).first()
                if stale:
                    session.delete(stale)
        except Exception as e:
            logger.debug(f"Could not remove stale TVDB mapping for {imdb_id}: {e}")
        from . import trakt_client
        trakt_result = trakt_client.get_show_data(imdb_id)
        if trakt_result:
            return trakt_result
        # Trakt also couldn't resolve this IMDB ID — try TMDB as final fallback.
        # This covers shows where TVDB has an incorrect IMDb mapping (e.g. same title,
        # different era) and Trakt's DB is also missing/stale for the ID.
        tmdb_api_key = _get_tmdb_api_key()
        if tmdb_api_key:
            logger.info(
                f"TVDB mismatch + Trakt failed for {imdb_id} — trying TMDB as final fallback"
            )
            return _fetch_tmdb_show_data(imdb_id, tmdb_api_key)
        return None

    # Build Trakt-compatible show dict
    show_data = _build_show_dict(raw, imdb_id, tvdb_id)

    # Aliases from translations
    translations = raw.get('translations', {}).get('nameTranslations', [])
    aliases = _extract_aliases(translations)
    if aliases:
        show_data['aliases'] = aliases

    # Seasons + episodes — prefer English paginated endpoint for correct titles
    airs_time = raw.get('airsTime')
    network = raw.get('originalNetwork')
    airs_timezone = network.get('timezone') if isinstance(network, dict) else None

    # If TVDB doesn't provide timezone, try to infer from network name or country
    if not airs_timezone and airs_time:
        network_name = network.get('name') if isinstance(network, dict) else None
        network_country = network.get('country') if isinstance(network, dict) else None
        inferred_tz = _infer_timezone_from_network(network_name, network_country)
        if inferred_tz:
            airs_timezone = inferred_tz
            logger.info(f"TVDB: Inferred timezone '{inferred_tz}' for network '{network_name}' country '{network_country}' (show {imdb_id})")
        else:
            logger.debug(f"TVDB: No timezone for show {imdb_id} (network: {network_name}), will use TMDB/Trakt batch fetch")

    # Get TMDB ID for batch fetching episode air dates
    tmdb_id = raw.get('remoteIds', [])
    tmdb_id = next((r.get('id') for r in tmdb_id if r.get('sourceName') == 'TheMovieDB.com'), None)

    seasons = _fetch_episodes_paginated(tvdb_id, airs_time, airs_timezone, imdb_id, tmdb_id)
    if not seasons:
        # Fallback to extended response (may have non-English titles)
        seasons = _extract_seasons_from_extended(raw)
    show_data['seasons'] = seasons

    # TVDB marks many canceled shows as 'Ended' — cross-check with Trakt
    # which reliably distinguishes 'canceled' from 'ended'.
    if show_data.get('status') == 'ended':
        trakt_status = _get_trakt_status(imdb_id)
        if trakt_status == 'canceled':
            logger.info(f"TVDB: correcting status for {imdb_id} from 'ended' to 'canceled' (Trakt)")
            show_data['status'] = 'canceled'

    return show_data


def _extract_tmdb_id_from_remote_ids(raw: dict) -> Optional[int]:
    """Pull the TMDB ID out of a TVDB series/movie response's own remoteIds,
    when present - more reliable than re-resolving it independently via
    TMDB's /find endpoint (which can fail to link an IMDb ID that TVDB's own
    cross-reference already has)."""
    remote_ids = raw.get('remoteIds', []) or raw.get('remote_ids', [])
    if not isinstance(remote_ids, list):
        return None
    for rid in remote_ids:
        if isinstance(rid, dict):
            source = (rid.get('sourceName', '') or '').lower()
            if 'tmdb' in source or 'themoviedb' in source:
                try:
                    return int(rid.get('id', ''))
                except (ValueError, TypeError):
                    return None
    return None


def _fetch_tmdb_title_only(tmdb_id: Optional[int], imdb_id: str, api_key: str, media_type: str) -> Optional[str]:
    """Minimal TMDB lookup for just a title - avoids the overhead of the full
    _fetch_tmdb_show_data / _fetch_tmdb_movie_data fallback fetchers below,
    which build out seasons/episodes/genres/etc. that aren't needed here.
    Falls back to resolving the TMDB ID from the IMDb ID only if the caller
    doesn't already have one from TVDB's own remoteIds."""
    if not tmdb_id:
        tmdb_id = _resolve_tmdb_id_from_imdb(imdb_id, api_key, media_type=media_type)
    if not tmdb_id:
        return None
    endpoint = 'tv' if media_type == 'show' else 'movie'
    try:
        resp = requests.get(
            f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}",
            params={'api_key': api_key},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        raw = resp.json()
        return raw.get('name') if media_type == 'show' else raw.get('title')
    except Exception as e:
        logger.debug(f"TMDB title-only lookup failed for {imdb_id}: {e}")
        return None


def _resolve_display_title(title: str, raw: dict, imdb_id: str, media_type: str) -> str:
    """When `title` (TVDB's primary name) contains non-Latin script, try to find
    a usable Latin-script title instead, in order:
      1. A TVDB nameTranslations 'eng' entry that's itself actually Latin-clean -
         not just the first 'eng' entry array-order, since TVDB's community data
         sometimes has the non-Latin name copied verbatim into the eng slot, or a
         mixed-script entry (e.g. a stray non-Latin word left in an otherwise
         English translation), ahead of a genuinely clean one further down.
      2. TMDB's own title for the same item, when TVDB has no clean translation -
         TMDB's editorial title is often present even when TVDB's
         community-contributed translations are missing or unusable.

    Detected via the presence of any non-Latin *letter*, not "lacks a Latin
    letter" — a title can contain both, e.g. Naruto's TVDB primary name is
    'NARUTO－ナルト－' (has plenty of Latin letters, but still half Japanese
    katakana); requiring the *absence* of Latin letters missed this case
    entirely. Also not just "any ASCII survives encoding" — a title like
    '怪獣8号' has a literal ASCII digit in it, so that check would call it
    "representable" too. Script-agnostic (Japanese, Korean, Chinese, Hindi,
    Cyrillic, Arabic, etc.) rather than enumerating specific scripts one at a
    time - see utilities.text_utils.has_non_latin_letter.

    Falls through to the original non-Latin title if nothing usable is found
    anywhere - the symlink-path layer (utilities/local_library_scan.py) has its
    own independent ID-based fallback for that case, so this never risks a
    folder collision either way, only display/scraping accuracy.
    """
    from utilities.text_utils import has_non_latin_letter
    if not title or not has_non_latin_letter(title):
        return title

    for t in (raw.get('translations', {}).get('nameTranslations') or []):
        candidate = t.get('name')
        if t.get('language') == 'eng' and candidate and not has_non_latin_letter(candidate):
            logger.info(
                f"TVDB primary name {title!r} is non-Latin — using clean TVDB "
                f"English translation {candidate!r} instead"
            )
            return candidate

    try:
        tmdb_api_key = _get_tmdb_api_key()
        if tmdb_api_key:
            tmdb_id = _extract_tmdb_id_from_remote_ids(raw)
            tmdb_title = _fetch_tmdb_title_only(tmdb_id, imdb_id, tmdb_api_key, media_type)
            if tmdb_title and not has_non_latin_letter(tmdb_title):
                logger.info(
                    f"TVDB primary name {title!r} is non-Latin with no clean TVDB "
                    f"translation — using TMDB title {tmdb_title!r} instead"
                )
                return tmdb_title
    except Exception as e:
        logger.debug(f"TMDB title fallback failed for {imdb_id}: {e}")

    logger.warning(
        f"TVDB primary name {title!r} is non-Latin and no clean English title "
        f"found via TVDB or TMDB — leaving as-is (symlink layer will use its "
        f"own ID-based fallback)"
    )
    return title


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
    for t in (raw.get('translations', {}).get('overviewTranslations') or []):
        if t.get('language') == 'eng':
            overview = t.get('overview', overview)
            break

    # Use TVDB's primary name as the canonical title. The "eng" nameTranslations
    # entry is sometimes a marketing AKA (e.g. "Gangs of Manila" for "Batang Quiapo")
    # rather than a true localization, so it must not override the primary name here —
    # it's still captured separately via _extract_aliases() for search matching.
    title = raw.get('name', '')

    # Exception: some series (commonly anime) have their primary TVDB name stored
    # partly or entirely in non-Latin script (e.g. Japanese/Chinese/Korean), with no
    # romanized or English form at all. Storing that as the canonical title breaks
    # every ASCII-dependent downstream consumer — scraper query normalization strips
    # it to an empty (or badly mangled) string, and symlink path generation strips it
    # down too. Fall back to a usable Latin-script title instead, when the primary
    # name actually contains non-Latin script.
    title = _resolve_display_title(title, raw, imdb_id, media_type='show')

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
        network_tz = raw['originalNetwork'].get('timezone')
        if network_tz:
            airs['timezone'] = network_tz
        elif airs.get('time'):
            # Try to infer timezone from network name or country
            network_name = raw['originalNetwork'].get('name')
            network_country = raw['originalNetwork'].get('country')
            inferred_tz = _infer_timezone_from_network(network_name, network_country)
            if inferred_tz:
                airs['timezone'] = inferred_tz

    show_data = {
        'title': title,
        'year': year,
        'ids': ids,
        'overview': overview,
        'runtime': raw.get('averageRuntime') or raw.get('defaultSeasonType'),
        'status': _map_status(raw.get('status', {}).get('name') if isinstance(raw.get('status'), dict) else raw.get('status')),
        'network': raw.get('originalNetwork', {}).get('name', '') if isinstance(raw.get('originalNetwork'), dict) else '',
        'genres': [g.get('name', '') for g in (raw.get('genres') or []) if isinstance(g, dict)],
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
    airs_timezone = network.get('timezone') if isinstance(network, dict) else None

    # If TVDB doesn't provide timezone, try to infer from network name or country
    if not airs_timezone and airs_time:
        network_name = network.get('name') if isinstance(network, dict) else None
        network_country = network.get('country') if isinstance(network, dict) else None
        inferred_tz = _infer_timezone_from_network(network_name, network_country)
        if inferred_tz:
            airs_timezone = inferred_tz

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
                'first_aired': _format_air_date(
                    ep.get('aired'), airs_time, airs_timezone,
                    imdb_id=imdb_id, season=season_number, episode=ep_num
                ),
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
    # No usable TVDB key: build seasons from TMDB. See get_show_data above.
    if not is_available():
        tmdb_api_key = _get_tmdb_api_key()
        if not tmdb_api_key:
            return None, None
        show = _fetch_tmdb_show_data(imdb_id, tmdb_api_key)
        if not show:
            return None, None
        seasons = {}
        for sn_str, sdata in (show.get('seasons') or {}).items():
            try:
                sn = int(sn_str)
            except (TypeError, ValueError):
                continue
            seasons[sn] = {
                'episode_count': sdata.get('episode_count', 0),
                'episodes': sdata.get('episodes', {}),
            }
        if seasons and not include_specials:
            seasons.pop(0, None)
        return (seasons, 'tmdb') if seasons else (None, None)

    tvdb_id = _resolve_tvdb_id(imdb_id, media_type='show')
    if not tvdb_id:
        logger.warning(f"TVDB: could not resolve IMDb {imdb_id} to TVDB ID for episodes, trying Trakt fallback")
        from . import trakt_client
        return trakt_client.get_show_seasons_and_episodes(imdb_id, include_specials=include_specials)

    # Try extended endpoint first
    url = f"{TVDB_BASE_URL}/series/{tvdb_id}/extended?meta=episodes"
    resp = _make_request(url)
    if not resp or resp.status_code != 200:
        logger.warning(f"TVDB episodes fetch failed for show {imdb_id} (TVDB ID: {tvdb_id}), trying Trakt fallback")
        from . import trakt_client
        return trakt_client.get_show_seasons_and_episodes(imdb_id, include_specials=include_specials)

    raw = resp.json().get('data', {})

    # Extract airs info for datetime construction
    airs_time = raw.get('airsTime')
    network = raw.get('originalNetwork')
    airs_timezone = network.get('timezone') if isinstance(network, dict) else None

    # If TVDB doesn't provide timezone, try to infer from network name or country
    if not airs_timezone and airs_time:
        network_name = network.get('name') if isinstance(network, dict) else None
        network_country = network.get('country') if isinstance(network, dict) else None
        inferred_tz = _infer_timezone_from_network(network_name, network_country)
        if inferred_tz:
            airs_timezone = inferred_tz

    # Get TMDB ID for batch fetching episode air dates
    tmdb_id = raw.get('remoteIds', [])
    tmdb_id = next((r.get('id') for r in tmdb_id if r.get('sourceName') == 'TheMovieDB.com'), None)

    # Prefer English paginated endpoint for correct episode titles
    seasons = _fetch_episodes_paginated(tvdb_id, airs_time, airs_timezone, imdb_id, tmdb_id)
    if not seasons:
        # Fallback to extended response (may have non-English titles)
        seasons = _extract_seasons_from_extended(raw)

    if seasons and not include_specials:
        seasons.pop(0, None)

    return seasons, 'tvdb' if seasons else None


def _fetch_episodes_paginated(tvdb_id: int, airs_time: str | None = None,
                              airs_timezone: str | None = None, imdb_id: str = None,
                              tmdb_id: int = None) -> Optional[dict]:
    """Fetch episodes via the paginated /episodes/default endpoint."""
    all_episodes: list = []
    page = 0
    while True:
        url = f"{TVDB_BASE_URL}/series/{tvdb_id}/episodes/default/eng?page={page}"
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

    # Batch-fetch episode air dates from TMDB/Trakt if no timezone available
    cached_air_dates = {}
    if not airs_timezone and imdb_id:
        cached_air_dates = _batch_fetch_episode_air_dates(imdb_id, tmdb_id)

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
                'first_aired': _format_air_date(
                    ep.get('aired'), airs_time, airs_timezone,
                    imdb_id=imdb_id, season=sn, episode=ep_num,
                    cached_air_dates=cached_air_dates
                ),
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
        logger.warning(f"TVDB metadata fetch failed for movie {imdb_id} (TVDB ID: {tvdb_id}), trying Trakt fallback")
        from . import trakt_client
        return trakt_client.get_movie_data(imdb_id)

    raw = resp.json().get('data', {})
    if not raw:
        logger.warning(f"TVDB returned empty data for movie {imdb_id} (TVDB ID: {tvdb_id}), trying Trakt fallback")
        from . import trakt_client
        return trakt_client.get_movie_data(imdb_id)

    # Validate that the TVDB movie's IMDb ID matches what we requested.
    # A stale cache entry could map the wrong TVDB ID to this IMDb ID.
    remote_ids_raw = raw.get('remoteIds', [])
    tvdb_imdb_id = None
    if isinstance(remote_ids_raw, list):
        for rid in remote_ids_raw:
            if isinstance(rid, dict):
                source_name = (rid.get('sourceName', '') or '').lower()
                rid_val = rid.get('id', '')
                if 'imdb' in source_name and rid_val and rid_val.startswith('tt'):
                    tvdb_imdb_id = rid_val
                    break
    if tvdb_imdb_id and tvdb_imdb_id != imdb_id:
        logger.warning(
            f"TVDB IMDb mismatch for movie: requested {imdb_id}, TVDB movie {tvdb_id} has {tvdb_imdb_id}. "
            f"Invalidating stale cache entry and falling back to Trakt."
        )
        try:
            with managed_session() as session:
                stale = session.query(TVDBToIMDBMapping).filter_by(imdb_id=imdb_id, tvdb_id=str(tvdb_id)).first()
                if stale:
                    session.delete(stale)
        except Exception as e:
            logger.debug(f"Could not remove stale TVDB mapping for {imdb_id}: {e}")
        from . import trakt_client
        return trakt_client.get_movie_data(imdb_id)

    data = _build_movie_dict(raw, imdb_id, tvdb_id)

    # Aliases from translations
    translations = raw.get('translations', {}).get('nameTranslations', [])
    aliases = _extract_aliases(translations)
    if aliases:
        data['aliases'] = aliases

    # Release dates (TVDB + TMDB supplement)
    releases = _extract_movie_releases(raw) or {}
    tmdb_api_key = _get_tmdb_api_key()
    if tmdb_api_key:
        tmdb_id = data.get('ids', {}).get('tmdb')
        if not tmdb_id:
            tmdb_id = _resolve_tmdb_id_from_imdb(imdb_id, tmdb_api_key, media_type='movie')
        if tmdb_id:
            tmdb_releases = _fetch_tmdb_release_dates(tmdb_id, tmdb_api_key)
            if tmdb_releases:
                for country, tmdb_entries in tmdb_releases.items():
                    existing_types = {
                        (r.get('type') or '').lower()
                        for r in releases.get(country, [])
                    }
                    for entry in tmdb_entries:
                        if (entry.get('type') or '').lower() not in existing_types:
                            releases.setdefault(country, []).append(entry)
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

    # Find title/overview. Unlike overview (always fine to prefer the English
    # translation when present), the primary name must NOT be unconditionally
    # replaced — a TVDB 'eng' nameTranslations entry is sometimes a wrong
    # marketing AKA rather than a true localization (e.g. "Gangs of Manila" for
    # "Batang Quiapo"), so only fall back to it when the primary name actually
    # contains non-Latin script and can't be used as-is. See
    # _resolve_display_title's docstring for the full detection/fallback chain
    # (TVDB clean translation, then TMDB, then leave as-is).
    title = raw.get('name', '')
    overview = raw.get('overview', '')
    title = _resolve_display_title(title, raw, imdb_id, media_type='movie')
    for t in raw.get('translations', {}).get('overviewTranslations', []) or []:
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
        'genres': [g.get('name', '') for g in (raw.get('genres') or []) if isinstance(g, dict)],
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


def _lookup_title_year_from_media_db(imdb_id: str) -> Tuple[Optional[str], Optional[int]]:
    """Look up title and year from media_items.db for a given IMDb ID."""
    import os
    import sqlite3 as _sqlite3
    db_path = os.path.join(os.environ.get('USER_DB_CONTENT', '/user/db_content'), 'media_items.db')
    try:
        conn = _sqlite3.connect(db_path)
        row = conn.execute(
            'SELECT title, year FROM media_items WHERE imdb_id = ? LIMIT 1',
            (imdb_id,)
        ).fetchone()
        conn.close()
        if row:
            return row[0], row[1]
    except Exception as e:
        logger.debug(f"Could not look up title/year for {imdb_id} from media_items.db: {e}")
    return None, None


def _search_tmdb_by_title(title: str, year: Optional[int], api_key: str, media_type: str) -> Optional[int]:
    """Search TMDB by title+year and return TMDB ID if a confident match is found.

    Requires exact title match (case-insensitive) and year within 1 year.
    Returns None if no confident match found.
    """
    try:
        endpoint = 'tv' if media_type == 'show' else 'movie'
        title_field = 'name' if media_type == 'show' else 'title'
        date_field = 'first_air_date' if media_type == 'show' else 'release_date'
        resp = requests.get(
            f"https://api.themoviedb.org/3/search/{endpoint}",
            params={'api_key': api_key, 'query': title},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        for result in resp.json().get('results', []):
            result_title = result.get(title_field, '')
            if result_title.lower() != title.lower():
                continue
            result_year = None
            date_str = result.get(date_field, '')
            if date_str and len(date_str) >= 4:
                try:
                    result_year = int(date_str[:4])
                except ValueError:
                    pass
            if year and result_year and abs(result_year - year) <= 1:
                logger.info(f"TMDB title search: matched '{title}' ({year}) → TMDB ID {result['id']}")
                return result['id']
    except Exception as e:
        logger.debug(f"TMDB title search error for '{title}': {e}")
    return None


def _fetch_tmdb_movie_data(imdb_id: str, api_key: str) -> Optional[dict]:
    """Fetch movie metadata from TMDB as fallback when TVDB cannot resolve the ID."""
    tmdb_id = _resolve_tmdb_id_from_imdb(imdb_id, api_key, media_type='movie')
    if not tmdb_id:
        title, year = _lookup_title_year_from_media_db(imdb_id)
        if title:
            tmdb_id = _search_tmdb_by_title(title, year, api_key, media_type='movie')
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

        genres = [g.get('name', '') for g in (raw.get('genres') or []) if isinstance(g, dict)]

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


def _fetch_tmdb_episodes(tmdb_id: int, api_key: str,
                         season_numbers: List[Optional[int]]) -> Optional[dict]:
    """Fetch episodes per season from TMDB, shaped like _fetch_episodes_paginated.

    Returns {season_number: {episode_number: {title, overview, runtime,
    first_aired, imdb_id, absolute}}}.

    TMDB gives air_date as a plain YYYY-MM-DD with no air time or network
    timezone, so first_aired is formatted date-only. Downstream treats a
    date-only value the same way it does for TVDB shows lacking airsTime.
    """
    if not tmdb_id:
        return None

    by_season: dict = {}
    for sn in season_numbers:
        if sn is None:
            continue
        try:
            resp = requests.get(
                f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{sn}",
                params={'api_key': api_key},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code != 200:
                logger.debug(f"TMDB season {sn} fetch failed for tmdb_id={tmdb_id}: {resp.status_code}")
                continue

            ep_dict: dict = {}
            for ep in (resp.json().get('episodes') or []):
                ep_num = ep.get('episode_number')
                if ep_num is None:
                    continue
                ep_dict[ep_num] = {
                    'title': ep.get('name', '') or '',
                    'overview': ep.get('overview', '') or '',
                    'runtime': ep.get('runtime') or 0,
                    'first_aired': _format_air_date(ep.get('air_date')),
                    'imdb_id': None,
                    'absolute': None,
                }
            if ep_dict:
                by_season[sn] = ep_dict
        except Exception as e:
            logger.debug(f"TMDB season {sn} error for tmdb_id={tmdb_id}: {e}")

    if by_season:
        total = sum(len(v) for v in by_season.values())
        logger.info(f"TMDB: fetched {total} episode(s) across {len(by_season)} season(s) "
                    f"for tmdb_id={tmdb_id}")
    return by_season or None


def _fetch_tmdb_show_data(imdb_id: str, api_key: str) -> Optional[dict]:
    """Fetch show metadata from TMDB as fallback when TVDB cannot resolve the ID."""
    tmdb_id = _resolve_tmdb_id_from_imdb(imdb_id, api_key, media_type='show')
    if not tmdb_id:
        title, year = _lookup_title_year_from_media_db(imdb_id)
        if title:
            tmdb_id = _search_tmdb_by_title(title, year, api_key, media_type='show')
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

        genres = [g.get('name', '') for g in (raw.get('genres') or []) if isinstance(g, dict)]

        # Build seasons dict, populating episodes from TMDB. Without episodes
        # the show cannot expand into wanted items, so a show-level-only result
        # is no more useful than none at all.
        episodes_by_season = _fetch_tmdb_episodes(
            tmdb_id, api_key,
            [s.get('season_number') for s in (raw.get('seasons') or [])],
        ) or {}

        seasons = {}
        for s in (raw.get('seasons') or []):
            sn = s.get('season_number')
            if sn is not None:
                fetched = episodes_by_season.get(sn) or {}
                seasons[str(sn)] = {
                    'number': sn,
                    'episode_count': len(fetched) or s.get('episode_count', 0),
                    'episodes': fetched,
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
