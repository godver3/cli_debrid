"""Episodes that are really movies.

Some shows list a film as a one-episode season: TVDB has Initial D's "Third
Stage" (105 min, the other episodes ~24 min) as S03E01. Its releases are named
like a movie ("Initial D Third Stage 2001 1080p") with no season or episode
number, so an S03E01 search and the S/E checks after it never find one.

TVDB's isMovie flag is used when set, but it isn't reliable (it is 0 for Third
Stage), so a lone long episode in a season of its own also counts.
"""
import logging
import re
import statistics
import threading
import time
from typing import Any, Dict, Optional

MIN_MOVIE_RUNTIME = 60       # minutes
RUNTIME_FACTOR = 2.0         # vs the show's median episode runtime
MIN_REFERENCE_EPISODES = 4   # episodes needed to trust that median

_CACHE_TTL = 600
_cache: Dict[str, tuple] = {}
_cache_lock = threading.Lock()


def _season(seasons: Dict[Any, Any], season: int) -> Optional[Dict[str, Any]]:
    data = seasons.get(season)
    if data is None:
        data = seasons.get(str(season))
    return data if isinstance(data, dict) else None


def _episodes(season_data: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    episodes = {}
    for number, ep in (season_data.get('episodes') or {}).items():
        try:
            number = int(number)
        except (TypeError, ValueError):
            continue
        if isinstance(ep, dict):
            episodes[number] = ep
    return episodes


def detect_movie_episode(seasons: Optional[Dict[Any, Any]], season: Optional[int],
                         episode: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """The episode's data (plus 'episode_number') if S/E is a movie, else None.

    episode=None asks about a whole season (a season-pack search), which counts
    when the season is a single movie episode.
    """
    if not seasons or not season or season < 1:
        return None
    season_data = _season(seasons, season)
    if not season_data:
        return None
    episodes = _episodes(season_data)
    if episode is None:
        if len(episodes) != 1:
            return None
        episode = next(iter(episodes))
    ep = episodes.get(episode)
    if not ep:
        return None

    if ep.get('is_movie'):
        return dict(ep, episode_number=episode)

    if len(episodes) != 1:
        return None
    runtime = ep.get('runtime') or 0
    if runtime < MIN_MOVIE_RUNTIME:
        return None
    reference = []
    for s_num, s_data in seasons.items():
        try:
            s_num = int(s_num)
        except (TypeError, ValueError):
            continue
        if s_num < 1 or s_num == season or not isinstance(s_data, dict):
            continue
        reference.extend(e.get('runtime') for e in _episodes(s_data).values() if (e.get('runtime') or 0) > 0)
    if len(reference) < MIN_REFERENCE_EPISODES:
        return None
    if runtime < RUNTIME_FACTOR * statistics.median(reference):
        return None
    return dict(ep, episode_number=episode)


def _year(first_aired: Any) -> Optional[int]:
    text = str(first_aired or '')
    return int(text[:4]) if len(text) >= 4 and text[:4].isdigit() else None


def movie_episode_info(imdb_id: Optional[str], season: Optional[int],
                       episode: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """detect_movie_episode for a show in cli_battery, with the movie's year.

    Returns {'episode_number', 'title', 'runtime', 'year', ...} or None. Show
    data is cached for a few minutes; any lookup failure means "not a movie".
    """
    if not imdb_id or not season:
        return None
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(imdb_id)
    if hit and now - hit[0] < _CACHE_TTL:
        seasons = hit[1]
    else:
        try:
            from cli_battery.app.direct_api import DirectAPI
            metadata, _ = DirectAPI.get_show_metadata(imdb_id)
            seasons = (metadata or {}).get('seasons') or {}
        except Exception as e:
            logging.debug(f"Movie-episode check: no show data for {imdb_id}: {e}")
            return None
        with _cache_lock:
            if len(_cache) > 500:
                _cache.clear()
            _cache[imdb_id] = (now, seasons)
    info = detect_movie_episode(seasons, season, episode)
    if info is None:
        return None
    info['year'] = _year(info.get('first_aired'))
    return info


def movie_search_title(show_title: str, episode_title: Optional[str]) -> str:
    """'Initial D' + 'Third Stage' -> 'Initial D Third Stage'. A title that
    already contains the show's name is used as is; a placeholder title
    ("Episode 1") or none at all gives just the show's name."""
    show_title = (show_title or '').strip()
    episode_title = (episode_title or '').strip()
    if not episode_title or re.fullmatch(r'(?:episode|ep)\.?\s*\d+', episode_title, re.IGNORECASE):
        return show_title
    if show_title and show_title.lower() in episode_title.lower():
        return episode_title
    return f"{show_title} {episode_title}".strip()
