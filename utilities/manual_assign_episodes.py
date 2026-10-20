"""Absolute-number matching for the manual (magnet) assign page.

The page's S/E pass only matches files named with the show's own season and
episode numbers. Anime packs are often numbered differently:

- absolute: "Show - 030.mkv" for what the show calls S02E05, or the other
  way round, "S02E05" files for a show set to an absolute episode order
  (everything in season 1);
- every episode under S01: "S01E30".

match_absolute_files() runs after the S/E pass and assigns what it can.
A file is only assigned when its number points at exactly one episode and
it is the only file doing so; anything less certain is left for the user.
"""
import logging
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from scraper.functions.anime_utils import absolute_from_counts, explicit_absolute_numbers


def absolute_numbers(seasons: Optional[Dict[Any, Any]]) -> Dict[Tuple[int, int], List[int]]:
    """{(season, episode): [absolute, ...]} for every regular-season episode.

    The provider's own absolute number comes first, then the count of earlier
    regular episodes plus the episode when that differs (TVDB's absolute
    numbering sometimes skips, e.g. Dragon Ball Z Kai season 2). Specials
    (S0) have no absolute number.
    """
    counts: Dict[int, int] = {}
    for s_num, s_data in (seasons or {}).items():
        try:
            s_num = int(s_num)
        except (TypeError, ValueError):
            continue
        if isinstance(s_data, dict):
            counts[s_num] = len(s_data.get('episodes') or {}) or s_data.get('episode_count') or 0

    result: Dict[Tuple[int, int], List[int]] = {}
    for s_num, s_data in (seasons or {}).items():
        try:
            s_num = int(s_num)
        except (TypeError, ValueError):
            continue
        if s_num <= 0 or not isinstance(s_data, dict):
            continue
        for e_num, ep in (s_data.get('episodes') or {}).items():
            try:
                e_num = int(e_num)
            except (TypeError, ValueError):
                continue
            provider_abs = ep.get('absolute') if isinstance(ep, dict) else None
            try:
                provider_abs = int(provider_abs) if provider_abs else None
            except (TypeError, ValueError):
                provider_abs = None
            numbers = [n for n in (provider_abs, absolute_from_counts(s_num, e_num, counts)) if n]
            if numbers:
                result[(s_num, e_num)] = list(dict.fromkeys(numbers))
    return result


def file_explicit_absolute(parsed: Dict[str, Any], filename: Optional[str],
                           seasons: Optional[Dict[Any, Any]] = None) -> List[int]:
    """Absolute numbers a file states next to its own season ("S13E01 (0422)").

    seasons is the show's season data, so a number inside the named season
    counts that season (see explicit_absolute_numbers)."""
    counts = {}
    for s_num, s_data in (seasons or {}).items():
        try:
            counts[int(s_num)] = len((s_data or {}).get('episodes') or {}) if isinstance(s_data, dict) else 0
        except (TypeError, ValueError):
            continue
    return explicit_absolute_numbers(filename or '', _ints(parsed.get('seasons')), _ints(parsed.get('episodes')),
                                     counts or None)


def explicit_absolute_conflicts(item_numbers: Optional[List[int]], file_numbers: List[int]) -> bool:
    """True when a file's stated absolute number is another episode's."""
    return bool(item_numbers and file_numbers and not set(item_numbers) & set(file_numbers))


def _ints(values: Iterable[Any]) -> List[int]:
    out = []
    for v in values or []:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


def _episode_ids(seasons: Optional[Dict[Any, Any]]) -> Dict[Tuple[int, int], Set[str]]:
    """{(season, episode): {'tvdb:<id>', 'tmdb:<id>'}} for episodes that carry IDs."""
    ids: Dict[Tuple[int, int], Set[str]] = {}
    for s_num, s_data in (seasons or {}).items():
        if not isinstance(s_data, dict):
            continue
        for e_num, ep in (s_data.get('episodes') or {}).items():
            if not isinstance(ep, dict):
                continue
            keys = {f"{k[:4]}:{ep[k]}" for k in ('tvdb_id', 'tmdb_id') if ep.get(k)}
            if keys:
                try:
                    ids[(int(s_num), int(e_num))] = keys
                except (TypeError, ValueError):
                    continue
    return ids


class _ShowNumbering:
    """Where a pack file's numbers land in the show's own numbering."""

    def __init__(self, show_seasons, alt_seasons=None):
        self.show_abs = absolute_numbers(show_seasons)
        # Which episodes each absolute number could mean. A number two
        # episodes claim (TVDB's absolute vs a plain count) is ambiguous.
        self.claims: Dict[int, Set[Tuple[int, int]]] = defaultdict(set)
        for pos, numbers in self.show_abs.items():
            for n in numbers:
                self.claims[n].add(pos)
        self.alt_abs = absolute_numbers(alt_seasons) if alt_seasons else {}
        self.alt_ids = _episode_ids(alt_seasons) if alt_seasons else {}
        self.id_to_pos: Dict[str, Tuple[int, int]] = {}
        for pos, keys in _episode_ids(show_seasons).items():
            for key in keys:
                self.id_to_pos.setdefault(key, pos)

    def _by_number(self, numbers: Iterable[int]) -> Optional[Tuple[int, int]]:
        positions: Set[Tuple[int, int]] = set()
        for n in numbers:
            positions |= self.claims.get(n, set())
        if len(positions) == 1:
            return next(iter(positions))
        if len(positions) > 1:
            logging.info(f"[Absolute] Absolute {sorted(set(numbers))} could be any of {sorted(positions)}; not guessing")
        return None

    def file_positions(self, parsed: Dict[str, Any], explicit_absolute: Optional[List[int]] = None) -> Set[Tuple[int, int]]:
        """The show's (season, episode) a parsed file name stands for.

        explicit_absolute: absolute numbers the name states next to its own
        season ("S13E01 (0422)"); they decide over the name's S/E."""
        seasons = _ints(parsed.get('seasons'))
        episodes = _ints(parsed.get('episodes'))
        positions: Set[Tuple[int, int]] = set()
        if explicit_absolute:
            pos = self._by_number(explicit_absolute)
            return {pos} if pos else positions
        if not episodes:
            return positions
        if not seasons:
            # "Show - 030": the number is already absolute
            for n in episodes:
                pos = self._by_number([n])
                if pos:
                    positions.add(pos)
            return positions
        for s in seasons:
            for e in episodes:
                pos = None
                if (s, e) in self.alt_abs:
                    # Numbered in the other (aired) order: same episode by ID,
                    # else by its absolute number
                    pos = next((self.id_to_pos[k] for k in self.alt_ids.get((s, e), ()) if k in self.id_to_pos), None)
                    if pos is None:
                        pos = self._by_number(self.alt_abs[(s, e)])
                elif (s, e) in self.show_abs:
                    pos = (s, e)
                elif s == 1:
                    # "S01E30" past the end of season 1: every episode under S01
                    pos = self._by_number([e])
                if pos:
                    positions.add(pos)
        return positions


def match_absolute_files(target_items: List[Dict[str, Any]],
                         parsed_files: List[Dict[str, Any]],
                         show_seasons: Optional[Dict[Any, Any]],
                         alt_seasons: Optional[Dict[Any, Any]] = None) -> int:
    """Assign unused files to unassigned episode items by absolute number.

    target_items / parsed_files are the manual assign page's structures
    (items with 'assigned' / 'suggested_file_path', files with 'parsed',
    'original' and 'used'). alt_seasons is another numbering the pack may
    use, normally the default aired order when the show is set to a
    different one. Every episode item gets 'absolute_number' for display.
    Returns how many items were assigned.
    """
    numbering = _ShowNumbering(show_seasons, alt_seasons)

    items_by_pos: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for item in target_items:
        if item.get('type') != 'episode':
            continue
        pos = (item.get('season_number'), item.get('episode_number'))
        numbers = numbering.show_abs.get(pos) or []
        item['absolute_number'] = numbers[0] if numbers else None
        if not item.get('assigned'):
            items_by_pos[pos] = item

    files_by_pos: Dict[Tuple[int, int], List[Dict[str, Any]]] = defaultdict(list)
    for f in parsed_files:
        if f.get('used'):
            continue
        explicit = file_explicit_absolute(f.get('parsed') or {}, (f.get('original') or {}).get('filename'),
                                          show_seasons)
        positions = numbering.file_positions(f.get('parsed') or {}, explicit) & set(items_by_pos)
        if len(positions) == 1:  # multi-episode files are left to the user
            files_by_pos[next(iter(positions))].append(f)

    assigned = 0
    for pos, item in items_by_pos.items():
        candidates = files_by_pos.get(pos, [])
        if len(candidates) != 1:
            if len(candidates) > 1:
                logging.warning(f"[Absolute] {len(candidates)} files fit {item.get('item_key')}; leaving it for "
                                f"the next pass: {[c['original'].get('path') for c in candidates]}")
            continue
        filename = candidates[0]['original'].get('filename')
        if not filename:
            continue
        item['suggested_file_path'] = filename
        item['assigned'] = True
        candidates[0]['used'] = True
        assigned += 1
        logging.info(f"[Absolute] Assigned '{candidates[0]['original'].get('path')}' to "
                     f"{item.get('item_key')} (absolute {item.get('absolute_number')})")
    return assigned
