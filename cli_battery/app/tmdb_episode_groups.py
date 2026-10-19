"""TMDB episode groups: alternative episode orders for a series.

TMDB keeps orders such as "Seasons (USA)", "Absolute" or a DVD order as
episode groups. Each group of a group set becomes a season (its `order`:
0 is usually Specials) and each episode's `order` (0-based) its number. Every
episode also carries its season/episode in TMDB's default order and its TMDB
episode ID, which is what lets library rows be moved between orders.
"""
from typing import Optional

import requests

from .logger_config import logger

TMDB_BASE_URL = "https://api.themoviedb.org/3"
REQUEST_TIMEOUT = 15
GROUP_TYPES = {
    1: 'Original air date', 2: 'Absolute', 3: 'DVD', 4: 'Digital', 5: 'Story arc',
    6: 'Production', 7: 'TV',
}

_http = requests.Session()


def _api_key() -> Optional[str]:
    from utilities.settings import get_setting
    return get_setting('TMDB', 'api_key') or None


def _get(path: str) -> Optional[dict]:
    api_key = _api_key()
    if not api_key:
        return None
    try:
        resp = _http.get(f"{TMDB_BASE_URL}{path}", params={'api_key': api_key}, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        logger.warning(f"TMDB request failed for {path}: {e}")
        return None
    if resp.status_code != 200:
        logger.warning(f"TMDB returned {resp.status_code} for {path}")
        return None
    return resp.json()


def list_episode_groups(tmdb_id) -> list:
    """[{'id', 'name', 'type', 'type_name', 'group_count', 'episode_count'}] for a TMDB series."""
    if not tmdb_id:
        return []
    data = _get(f"/tv/{tmdb_id}/episode_groups") or {}
    return [{
        'id': g.get('id'),
        'name': g.get('name'),
        'type': g.get('type'),
        'type_name': GROUP_TYPES.get(g.get('type'), 'Other'),
        'group_count': g.get('group_count'),
        'episode_count': g.get('episode_count'),
    } for g in data.get('results') or [] if g.get('id')]


def seasons_from_episode_group(group_data: dict) -> Optional[dict]:
    """Convert a TMDB /tv/episode_group/<id> response into cli_battery's seasons
    shape: {season: {'episode_count', 'episodes': {number: {...}}}}."""
    seasons: dict = {}
    for group in group_data.get('groups') or []:
        season_number = group.get('order')
        if not isinstance(season_number, int):
            continue
        episodes = {}
        for ep in group.get('episodes') or []:
            if not isinstance(ep.get('order'), int):
                continue
            air_date = ep.get('air_date')
            episodes[ep['order'] + 1] = {
                'title': ep.get('name', ''),
                'overview': ep.get('overview', ''),
                'runtime': ep.get('runtime') or 0,
                'first_aired': f"{air_date}T00:00:00.000Z" if air_date else None,
                'imdb_id': None,
                'tmdb_id': str(ep['id']) if ep.get('id') else None,
                # Position in TMDB's default order, for moving library rows
                'source_season': ep.get('season_number'),
                'source_episode': ep.get('episode_number'),
                # A single-group "Absolute" set numbers straight through
                'absolute': ep['order'] + 1 if group_data.get('type') == 2 else None,
            }
        if season_number in seasons:
            # Two groups claiming one season number: keep the first, say so
            logger.warning(f"TMDB episode group {group_data.get('id')}: duplicate season {season_number} "
                           f"('{group.get('name')}') ignored")
            continue
        seasons[season_number] = {'episode_count': len(episodes), 'episodes': episodes}
    return seasons or None


def get_seasons_for_episode_group(group_id: str) -> Optional[dict]:
    data = _get(f"/tv/episode_group/{group_id}")
    if not data:
        return None
    return seasons_from_episode_group(data)


MAX_EXTERNAL_ID_LOOKUPS = 400


def add_tvdb_ids(tmdb_show_id, seasons: dict, positions) -> int:
    """Fill in 'tvdb_id' for the given (season, episode) positions of a group's
    seasons dict, from TMDB's per-episode external IDs (looked up by each
    episode's position in TMDB's default order). Lets a group join a TVDB
    numbered library on IDs instead of air date and title. One request per
    episode, so only the positions asked for, at most MAX_EXTERNAL_ID_LOOKUPS.
    Returns how many IDs were added.
    """
    from concurrent.futures import ThreadPoolExecutor

    wanted = []
    for season, episode in positions:
        ep = ((seasons.get(season) or {}).get('episodes') or {}).get(episode)
        if ep and not ep.get('tvdb_id') and ep.get('source_season') is not None and ep.get('source_episode') is not None:
            wanted.append(ep)
    if len(wanted) > MAX_EXTERNAL_ID_LOOKUPS:
        logger.info(f"TMDB {tmdb_show_id}: looking up TVDB IDs for the first {MAX_EXTERNAL_ID_LOOKUPS} "
                    f"of {len(wanted)} unmatched episodes")
        wanted = wanted[:MAX_EXTERNAL_ID_LOOKUPS]

    def lookup(ep):
        data = _get(f"/tv/{tmdb_show_id}/season/{ep['source_season']}/episode/{ep['source_episode']}/external_ids")
        return ep, (data or {}).get('tvdb_id')

    added = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for ep, tvdb_id in pool.map(lookup, wanted):
            if tvdb_id:
                ep['tvdb_id'] = str(tvdb_id)
                added += 1
    return added
