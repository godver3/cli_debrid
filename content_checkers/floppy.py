"""Floppy (self-hosted media tracker, github.com/dannyvfilms/Floppy) content sources.

Floppy Watchlist: the user's tracked movies, shows and anime with the chosen
statuses (0 Planning = watchlist, 1 In progress, 2 Paused, 3 Completed,
4 Dropped) from GET /api/v1/media/{movie|tv|anime}/?status=...
Floppy Lists: custom lists, GET /api/v1/lists/{id}/items/.

Auth: the user's API token in the X-API-Key header. Entries carry
item.source + item.media_id (tmdb for movies/shows, mal for anime) and
sometimes item.ids with external ids.
"""
import logging
from typing import Any, Dict, List, Optional, Tuple

from routes.api_tracker import api
from utilities.settings import get_setting
from content_checkers.tracker_common import gate_and_merge, resolve_imdb

REQUEST_TIMEOUT = 30
PAGE_SIZE = 200
MAX_PAGES = 100
STATUS_CODES = {'Planning': 0, 'In progress': 1, 'Paused': 2, 'Completed': 3, 'Dropped': 4}


def get_floppy_config() -> Optional[Dict[str, str]]:
    base_url = (get_setting('Floppy', 'url', '') or '').strip().rstrip('/')
    api_key = (get_setting('Floppy', 'api_key', '') or '').strip()
    if not base_url or not api_key:
        logging.warning("Floppy is not configured (Additional Settings → Tracking accounts → Floppy).")
        return None
    return {'base_url': base_url, 'api_key': api_key}


def _get(path: str, params=None) -> Optional[Dict[str, Any]]:
    config = get_floppy_config()
    if not config:
        return None
    url = f"{config['base_url']}/api/v1{path}"
    try:
        response = api.get(url, headers={'X-API-Key': config['api_key'], 'Accept': 'application/json'},
                           params=params, timeout=REQUEST_TIMEOUT)
        if response.status_code in (401, 403):
            logging.error(f"Floppy rejected the API key ({response.status_code}) for {path}")
            return None
        response.raise_for_status()
        return response.json()
    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching from Floppy ({url}): {e}")
        return None
    except ValueError as e:
        logging.error(f"Floppy returned invalid JSON for {path}: {e}")
        return None


def _get_all(path: str, params=None) -> Optional[List[Dict[str, Any]]]:
    """Every result of a paginated Floppy endpoint; None when the first page fails."""
    results: List[Dict[str, Any]] = []
    for page in range(MAX_PAGES):
        page_params = list(params or []) + [('limit', PAGE_SIZE), ('offset', page * PAGE_SIZE)]
        data = _get(path, page_params)
        if data is None:
            return None if page == 0 else results
        batch = data.get('results') or []
        results.extend(batch)
        if not batch or not (data.get('pagination') or {}).get('next'):
            break
    return results


def _entry(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A Floppy tracked/list row -> {'imdb_id', 'media_type', 'season'} or None."""
    item = row.get('item') or {}
    floppy_type = (item.get('media_type') or '').lower()
    if floppy_type == 'movie':
        media_type = 'movie'
    elif floppy_type in ('tv', 'season', 'episode'):
        media_type = 'tv'
    elif floppy_type == 'anime':
        # Anime is keyed by MyAnimeList; usable only when Floppy also has an
        # external id. Anime films resolve as movies through TMDB/IMDb.
        media_type = 'tv'
    else:
        return None
    ids = dict(item.get('ids') or {})
    source = (item.get('source') or '').lower()
    if source in ('tmdb', 'tvdb', 'imdb') and item.get('media_id'):
        ids.setdefault(source, item.get('media_id'))
    imdb_id = resolve_imdb(ids, media_type)
    if not imdb_id and floppy_type == 'anime':
        imdb_id = resolve_imdb(ids, 'movie')
        media_type = 'movie' if imdb_id else media_type
    if not imdb_id:
        logging.warning(f"Skipping Floppy item without a usable id: {item.get('title') or '?'} "
                        f"({source}:{item.get('media_id')})")
        return None
    season = None
    if floppy_type in ('season', 'episode') and item.get('season_number') not in (None, ''):
        try:
            season = int(item['season_number'])
        except (TypeError, ValueError):
            season = None
    return {'imdb_id': imdb_id, 'media_type': media_type, 'season': season}


def get_wanted_from_floppy_watchlist(source_config: Dict[str, Any], versions: Dict[str, bool],
                                     unblacklist: bool = False) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    statuses = source_config.get('floppy_statuses') or ['Planning']
    if isinstance(statuses, str):
        statuses = [s.strip() for s in statuses.split(',') if s.strip()]
    codes = [STATUS_CODES[s] for s in statuses if s in STATUS_CODES] or [0]
    media_filter = (source_config.get('media_type') or 'All').lower()
    floppy_types = []
    if media_filter in ('all', 'movies'):
        floppy_types.append('movie')
    if media_filter in ('all', 'shows'):
        floppy_types += ['tv', 'anime']
    rows: List[Dict[str, Any]] = []
    for floppy_type in floppy_types:
        batch = _get_all(f'/media/{floppy_type}/', [('status', c) for c in codes])
        if batch is None:
            if floppy_type == floppy_types[0]:
                return []
            continue
        rows.extend(batch)
    entries = [e for e in (_entry(r) for r in rows) if e]
    items = gate_and_merge(entries, 'Floppy', unblacklist=unblacklist)
    logging.info(f"Found {len(items)} items in Floppy ({', '.join(statuses)})")
    return [(items, versions)]


def get_wanted_from_floppy_lists(floppy_list_ids: str, versions: Dict[str, bool],
                                 unblacklist: bool = False) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    out = []
    for list_id in [x.strip() for x in str(floppy_list_ids or '').split(',') if x.strip()]:
        if not list_id.isdigit():
            logging.error(f"Floppy list id must be a number, got {list_id!r}")
            continue
        rows = _get_all(f'/lists/{list_id}/items/')
        if rows is None:
            logging.error(f"Failed to fetch Floppy list {list_id}")
            continue
        entries = [e for e in (_entry(r) for r in rows) if e]
        items = gate_and_merge(entries, f'Floppy list {list_id}', unblacklist=unblacklist)
        logging.info(f"Found {len(items)} items in Floppy list {list_id}")
        out.append((items, versions))
    return out


def test_connection() -> Tuple[bool, str]:
    data = _get('/lists/', [('limit', 1)])
    if data is None:
        return False, 'Could not reach Floppy with this URL and API key'
    return True, 'Connected'
