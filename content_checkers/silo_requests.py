"""Silo Requests content source: what people requested in Silo
(github.com/Silo-Server/silo-server), read from GET /api/v2/admin/requests.

Silo's own request feature hands approved requests to a Sonarr/Radarr or Seerr
integration. With none set up, an approved request stays "approved", and this
source picks it up from there. Requests still waiting for approval are added
only with include_pending. Finished, declined, cancelled and failed requests
are left out.

Uses Silo's URL and admin API key from Settings → File Management (Silo as the
media server). Requests carry tmdb_id/imdb_id/tvdb_id and, for a series, the
requested seasons.
"""
import logging
from typing import Any, Dict, List, Optional, Tuple

from content_checkers.tracker_common import gate_and_merge, resolve_imdb

ACTIVE_STATUSES = ('approved', 'queued', 'downloading')
MAX_PAGES = 200
PAGE_SIZE = 50  # Silo's maximum for this listing


_last_error = {'message': ''}


def _fail(message: str):
    _last_error['message'] = message
    logging.error(f"Silo Requests: {message}")
    return None


def _requests_from_silo() -> Optional[List[Dict[str, Any]]]:
    """Every active request; None when Silo couldn't be read."""
    from utilities import silo_functions as silo
    if not silo.is_silo_mode():
        return _fail("Silo is not the media server, or has no URL/API key (Settings → File Management)")
    url, key = silo.silo_config()
    rows, cursor = [], None
    try:
        for _ in range(MAX_PAGES):
            params = {'outcome': 'active', 'limit': PAGE_SIZE}
            if cursor:
                params['cursor'] = cursor
            r = silo._request('GET', '/api/v2/admin/requests', url, key, params=params, timeout=30)
            if r.status_code != 200:
                return _fail(f"listing failed (HTTP {r.status_code}): {silo._problem(r)}")
            body = r.json() or {}
            rows += body.get('items') or []
            cursor = (body.get('page') or {}).get('next_cursor')
            if not (body.get('page') or {}).get('has_more') or not cursor:
                break
    except Exception as e:
        return _fail(str(e))
    return rows


def _entries(req: Dict[str, Any], partial: bool) -> List[Dict[str, Any]]:
    """One gate_and_merge entry per requested season (or one for the whole show/movie)."""
    media_type = 'movie' if req.get('media_type') == 'movie' else 'tv'
    ids = {k: req.get(f'{k}_id') for k in ('imdb', 'tmdb', 'tvdb') if req.get(f'{k}_id')}
    imdb_id = resolve_imdb(ids, media_type)
    if not imdb_id:
        logging.warning(f"Silo Requests: skipping {req.get('title') or '?'} (no usable id: {ids})")
        return []
    seasons = [s for s in (req.get('seasons') or []) if isinstance(s, int)]
    if media_type == 'tv' and partial and seasons:
        return [{'imdb_id': imdb_id, 'media_type': media_type, 'season': s} for s in seasons]
    return [{'imdb_id': imdb_id, 'media_type': media_type, 'season': None}]


def get_wanted_from_silo_requests(source_config: Dict[str, Any], versions: Dict[str, bool],
                                  unblacklist: bool = False) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    rows = _requests_from_silo()
    if rows is None:
        return []
    statuses = set(ACTIVE_STATUSES) | ({'pending'} if source_config.get('include_pending') else set())
    media_filter = (source_config.get('media_type') or 'All').lower()
    partial = source_config.get('allow_partial_requests', True)
    entries = []
    for req in rows:
        if (req.get('status') or '') not in statuses:
            continue
        kind = 'movies' if req.get('media_type') == 'movie' else 'shows'
        if media_filter not in ('all', kind):
            continue
        entries += _entries(req, partial)
    items = gate_and_merge(entries, 'Silo Requests', unblacklist=unblacklist)
    logging.info(f"Found {len(items)} items in Silo requests ({', '.join(sorted(statuses))})")
    return [(items, versions)]


def test_connection() -> Tuple[bool, str]:
    rows = _requests_from_silo()
    if rows is None:
        return False, f"Couldn't read Silo's requests: {_last_error['message']}"
    return True, f"Connected to Silo ({len(rows)} open request(s))"
