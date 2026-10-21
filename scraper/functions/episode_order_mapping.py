"""TVDB aired numbering vs. a show's chosen episode order.

Releases are named in TVDB's aired numbering. A show set to another order
(DVD, absolute, a TMDB episode group; cli_battery/app/episode_order.py)
numbers the same episodes differently: One Piece's DVD S15E01 is aired
S13E41, and a release named "S15E01" is aired S15E01, a different episode.
The scraper, filter and matcher translate through this mapping. Episodes are
joined by TVDB/TMDB episode ID, then by a unique absolute number.
"""
import logging
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

Position = Tuple[int, int]

_ORDER_TTL = 60          # how long a show's chosen order is trusted before it is read again
_MAPPING_TTL = 3600
_FAILED_TTL = 300
# imdb_id -> (order read at, order, mapping built at, mapping)
_cache: Dict[str, Tuple[float, Optional[str], float, Optional['OrderMapping']]] = {}
_cache_lock = threading.Lock()


def _regular_episodes(seasons: Optional[Dict[Any, Any]]) -> Dict[Position, Dict[str, Any]]:
    out: Dict[Position, Dict[str, Any]] = {}
    for s_num, s_data in (seasons or {}).items():
        try:
            s_num = int(s_num)
        except (TypeError, ValueError):
            continue
        if s_num <= 0 or not isinstance(s_data, dict):
            continue
        for e_num, ep in (s_data.get('episodes') or {}).items():
            try:
                out[(s_num, int(e_num))] = ep if isinstance(ep, dict) else {}
            except (TypeError, ValueError):
                continue
    return out


def _ids(ep: Dict[str, Any]) -> Set[str]:
    return {f"{k[:4]}:{ep[k]}" for k in ('tvdb_id', 'tmdb_id') if ep.get(k)}


def _absolute(ep: Dict[str, Any]) -> Optional[int]:
    try:
        value = int(ep.get('absolute') or ep.get('absolute_number') or 0)
    except (TypeError, ValueError):
        return None
    return value or None


class OrderMapping:
    """Regular-season episodes joined between aired numbering and the show's order."""

    def __init__(self, order_seasons: Optional[Dict[Any, Any]], aired_seasons: Optional[Dict[Any, Any]]):
        order_eps = _regular_episodes(order_seasons)
        aired_eps = _regular_episodes(aired_seasons)
        self.order_counts: Dict[int, int] = {}
        for s, _ in order_eps:
            self.order_counts[s] = self.order_counts.get(s, 0) + 1
        self.aired_by_season: Dict[int, List[int]] = {}
        for s, e in sorted(aired_eps):
            self.aired_by_season.setdefault(s, []).append(e)

        by_id: Dict[str, Position] = {}
        by_abs: Dict[int, List[Position]] = {}
        for pos, ep in order_eps.items():
            for key in _ids(ep):
                by_id.setdefault(key, pos)
            n = _absolute(ep)
            if n:
                by_abs.setdefault(n, []).append(pos)

        self.aired_to_order: Dict[Position, Position] = {}
        for pos, ep in aired_eps.items():
            target = next((by_id[k] for k in _ids(ep) if k in by_id), None)
            if target is None:
                n = _absolute(ep)
                if n and len(by_abs.get(n, [])) == 1:
                    target = by_abs[n][0]
            if target is not None:
                self.aired_to_order[pos] = target
        self.order_to_aired: Dict[Position, Position] = {}
        for aired, order in self.aired_to_order.items():
            self.order_to_aired.setdefault(order, aired)

    def order_of(self, season: int, episode: int) -> Optional[Position]:
        """The show's (season, episode) for aired S/E; None if aired has no such episode."""
        return self.aired_to_order.get((season, episode))

    def aired_of(self, season: int, episode: int) -> Optional[Position]:
        """Aired (season, episode) for the show's S/E; None if unknown."""
        return self.order_to_aired.get((season, episode))

    def aired_positions(self, seasons: Iterable[int], episodes: Iterable[int] = ()) -> Set[Position]:
        """Aired episodes a name covers: its seasons' listed episodes, or whole seasons."""
        episodes = list(episodes or [])
        positions: Set[Position] = set()
        for s in seasons or []:
            for e in (episodes or self.aired_by_season.get(s, [])):
                if (s, e) in self.aired_to_order:
                    positions.add((s, e))
        return positions

    def order_seasons_held(self, aired_positions: Iterable[Position]) -> List[int]:
        """The show's seasons all of whose episodes are among aired_positions."""
        held: Dict[int, int] = {}
        for pos in set(aired_positions):
            target = self.aired_to_order.get(pos)
            if target:
                held[target[0]] = held.get(target[0], 0) + 1
        return sorted(s for s, n in held.items() if n >= self.order_counts.get(s, 0) > 0)


def order_mapping(imdb_id: Optional[str]) -> Optional[OrderMapping]:
    """The mapping for a show set to a non-default episode order; None for the
    default order, or when either episode list can't be fetched."""
    if not imdb_id:
        return None
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(imdb_id)
    if hit and now - hit[0] < _ORDER_TTL:
        return hit[3]

    from cli_battery.app.episode_order import fetch_seasons_for_order, show_episode_order
    order = show_episode_order(imdb_id)
    mapping = None
    built_at = now
    if order:
        if hit and hit[1] == order and hit[3] is not None and now - hit[2] < _MAPPING_TTL:
            mapping, built_at = hit[3], hit[2]
        elif hit and hit[1] == order and hit[3] is None and now - hit[2] < _FAILED_TTL:
            built_at = hit[2]
        else:
            try:
                order_seasons = fetch_seasons_for_order(imdb_id, order)
                aired_seasons = fetch_seasons_for_order(imdb_id, 'default')
                if order_seasons and aired_seasons:
                    mapping = OrderMapping(order_seasons, aired_seasons)
                    logging.info(f"Episode order {order} for {imdb_id}: "
                                 f"{len(mapping.aired_to_order)} aired episodes mapped")
            except Exception as e:
                logging.error(f"Could not map aired numbering to episode order {order} for {imdb_id}: {e}",
                              exc_info=True)
    with _cache_lock:
        if len(_cache) > 500:
            _cache.clear()
        _cache[imdb_id] = (now, order, built_at, mapping)
    return mapping


def clear_order_mapping_cache(imdb_id: Optional[str] = None) -> None:
    with _cache_lock:
        if imdb_id is None:
            _cache.clear()
        else:
            _cache.pop(imdb_id, None)
