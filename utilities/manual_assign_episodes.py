"""Absolute-number matching for the manual (magnet) assign page.

The page's S/E pass only matches files named with the show's own season and
episode numbers. Anime packs are often numbered differently:

- absolute: "Show - 030.mkv" for what the show calls S02E05, or the other
  way round, "S02E05" files for a show set to an absolute episode order
  (everything in season 1);
- every episode under S01: "S01E30";
- a bare number PTT leaves in the title: "[CBT] Gintama 003 [DVDrip].mkv".

match_absolute_files() runs after the S/E pass and assigns what it can.
A file is only assigned when its number points at exactly one episode and
it is the only file doing so; anything less certain is left for the user.
"""
import logging
import os
import re
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
    result: Dict[Tuple[int, int], List[int]] = {}
    for pos, (provider_abs, counted) in absolute_number_sources(seasons).items():
        numbers = [n for n in (provider_abs, counted) if n]
        if numbers:
            result[pos] = list(dict.fromkeys(numbers))
    return result


def absolute_number_sources(seasons: Optional[Dict[Any, Any]]
                            ) -> Dict[Tuple[int, int], Tuple[Optional[int], Optional[int]]]:
    """{(season, episode): (provider absolute, counted absolute)}, see absolute_numbers."""
    counts: Dict[int, int] = {}
    for s_num, s_data in (seasons or {}).items():
        try:
            s_num = int(s_num)
        except (TypeError, ValueError):
            continue
        if isinstance(s_data, dict):
            counts[s_num] = len(s_data.get('episodes') or {}) or s_data.get('episode_count') or 0

    result: Dict[Tuple[int, int], Tuple[Optional[int], Optional[int]]] = {}
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
            result[(s_num, e_num)] = (provider_abs, absolute_from_counts(s_num, e_num, counts) or None)
    return result


# A title ending in a bare number: "Gintama 003" (PTT reads no episode there)
_TRAILING_NUMBER_RE = re.compile(r'^(?P<prefix>.*?\S)[\s._-]+(?P<number>\d{1,4})$')

# Numbered BD/fansub extras whose trailing digits PTT (or Phase 2) can read as
# an episode: NCED/NCOP, OP/ED, Menu, PV, CM, CleanOP/ED. Not OVA/Special —
# those are often real assignable content (season 0 / specials).
_ANIME_OP_ED_RE = re.compile(
    r'(?<![a-z0-9])(?:nc\s*op|nc\s*ed|clean\s*op|clean\s*ed)(?![a-z])|'
    r'(?<![a-z0-9])(?:opening|ending)\s*\d|'
    r'(?<![a-z0-9])(?:op|ed|menu|pv|cm)\s*\d',
    re.IGNORECASE,
)


def is_anime_op_ed_extra(filename: Optional[str]) -> bool:
    """True for numbered anime OP/ED/menu/PV extras that must not auto-assign."""
    if not filename:
        return False
    stem = os.path.splitext(os.path.basename(filename.replace('\\', '/')))[0]
    return bool(_ANIME_OP_ED_RE.search(stem))


def phase2_title_too_weak(cleaned_filename: str, series_title: Optional[str]) -> bool:
    """True when Phase 2 fuzzy match would only see the show name (or nothing).

    After S/E tags are stripped, leftovers like "gintama" token_set_ratio-match
    every episode title that contains the show name at 100.
    """
    cleaned = re.sub(r'[\s._-]+', ' ', (cleaned_filename or '').strip().lower()).strip()
    series = re.sub(r'[\s._-]+', ' ', (series_title or '').strip().lower()).strip()
    return not cleaned or (bool(series) and cleaned == series)


def pack_bare_numbers(parsed_files: List[Dict[str, Any]]) -> Dict[int, int]:
    """{id(file): episode} for files numbered only by a bare number after the title.

    PTT reads "[CBT] Gintama 003 [DVDrip].mkv" as the title "Gintama 003" with
    no episode. The trailing number counts only when the pack backs it up:
    the most common title before the number, shared by at least two files
    with different numbers. A lone "Mob Psycho 100" or extras named
    "NCOP 1" next to the episodes don't read as episodes.
    """
    groups: Dict[str, Dict[int, int]] = defaultdict(dict)
    for f in parsed_files:
        parsed = f.get('parsed') or {}
        if parsed.get('seasons') or parsed.get('episodes'):
            continue
        m = _TRAILING_NUMBER_RE.match((parsed.get('title') or '').strip())
        if not m:
            continue
        prefix = re.sub(r'[\s._-]+', ' ', m.group('prefix')).strip().casefold()
        groups[prefix][id(f)] = int(m.group('number'))
    best = max(groups.values(), key=len, default={})
    if len(best) < 2 or len(set(best.values())) < 2:
        return {}
    if sum(len(g) == len(best) for g in groups.values()) > 1:
        return {}  # two equally common titles: no telling which is the episodes
    return {f_id: n for f_id, n in best.items() if n > 0}


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


def file_position_label(parsed: Dict[str, Any], explicit_absolute: Optional[List[int]] = None) -> str:
    """Short label for what a file's name says it holds: "S02E05", "S01E01-02",
    "E07" or "#050"; empty when the name has no numbers."""
    numbers = sorted(set(_ints(explicit_absolute or [])))
    if numbers:
        return f'#{numbers[0]:03d}' + (f'-{numbers[-1]:03d}' if len(numbers) > 1 else '')
    episodes = sorted(set(_ints(parsed.get('episodes') or [])))
    if not episodes:
        return ''
    span = f'E{episodes[0]:02d}' + (f'-{episodes[-1]:02d}' if len(episodes) > 1 else '')
    seasons = sorted(set(_ints(parsed.get('seasons') or [])))
    return (f'S{seasons[0]:02d}' if len(seasons) == 1 else '') + span


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
        # Which episodes each absolute number could mean, by the provider's
        # numbering and by a plain count. Once the provider's numbering skips
        # (TVDB files One Piece 590 under specials), every later number is
        # claimed by two episodes; prefer_numbering() picks one per pack.
        self.provider_claims: Dict[int, Set[Tuple[int, int]]] = defaultdict(set)
        self.counted_claims: Dict[int, Set[Tuple[int, int]]] = defaultdict(set)
        for pos, (provider_abs, counted) in absolute_number_sources(show_seasons).items():
            if provider_abs:
                self.provider_claims[provider_abs].add(pos)
            if counted:
                self.counted_claims[counted].add(pos)
        self.special_slots = _special_slots(show_seasons, self.provider_claims)
        self.preferred: Optional[Dict[int, Set[Tuple[int, int]]]] = None
        self.pack_numbers: Set[int] = set()  # absolute numbers the pack's names state
        self.quiet = False
        self.alt_abs = absolute_numbers(alt_seasons) if alt_seasons else {}
        self.alt_ids = _episode_ids(alt_seasons) if alt_seasons else {}
        self.id_to_pos: Dict[str, Tuple[int, int]] = {}
        for pos, keys in _episode_ids(show_seasons).items():
            for key in keys:
                self.id_to_pos.setdefault(key, pos)

    def _by_number(self, numbers: Iterable[int], from_name: bool = True) -> Optional[Tuple[int, int]]:
        numbers = list(numbers)
        if from_name:
            self.pack_numbers.update(numbers)
        positions: Set[Tuple[int, int]] = set()
        for n in numbers:
            positions |= self.provider_claims.get(n, set()) | self.counted_claims.get(n, set())
        if self.special_slots.intersection(numbers) and self.preferred is not self.counted_claims:
            # "One Piece - 0590" is the special unless the pack counts straight through
            positions = set()
        elif len(positions) > 1 and self.preferred is not None:
            positions = set().union(*(self.preferred.get(n, set()) for n in numbers))
        if len(positions) == 1:
            return next(iter(positions))
        if len(positions) > 1 and not self.quiet:
            logging.info(f"[Absolute] Absolute {sorted(set(numbers))} could be any of {sorted(positions)}; not guessing")
        return None

    def prefer_numbering(self) -> None:
        """Settle numbers both numberings claim by the one the pack follows.

        The pack follows the count when it has numbers only the count uses
        (a pack numbered straight through a skip in TVDB's numbering), and
        the provider when it has numbers only the provider uses or leaves
        out a count-only number that a special aired in ("[Lia] One Piece"
        has no 590, the Dream 9 crossover TVDB files as S00E39). A pack that has
        a number the provider skips for a special proves nothing: "0590" is
        the special in a broadcast-numbered pack and S16E12 in a counted
        one. Evidence both ways, or none, leaves those numbers to the user.
        """
        pack_numbers = self.pack_numbers
        if not pack_numbers:
            return
        provider_only = set(self.provider_claims) - set(self.counted_claims)
        counted_only = set(self.counted_claims) - set(self.provider_claims)
        low, high = min(pack_numbers), max(pack_numbers)
        follows_count = bool(pack_numbers & (counted_only - self.special_slots))
        # A count-only number missing from the pack is only evidence where a special
        # aired in that slot; elsewhere the pack may just be missing that file.
        follows_provider = (bool(pack_numbers & provider_only)
                            or any(low < n < high and n not in pack_numbers
                                   for n in counted_only & self.special_slots))
        if follows_count != follows_provider:
            self.preferred = self.counted_claims if follows_count else self.provider_claims
            logging.info(f"[Absolute] Pack numbers follow the {'counted' if follows_count else 'provider'} "
                         f"absolute numbering")

    def file_positions(self, parsed: Dict[str, Any], explicit_absolute: Optional[List[int]] = None,
                       bare_number: Optional[int] = None) -> Set[Tuple[int, int]]:
        """The show's (season, episode) a parsed file name stands for.

        explicit_absolute: absolute numbers the name states next to its own
        season ("S13E01 (0422)"); they decide over the name's S/E.
        bare_number: the pack's trailing title number ("Gintama 003"), see
        pack_bare_numbers. Multi-episode files ("001-002") give one position
        per episode, or none when any of them doesn't resolve."""
        seasons = _ints(parsed.get('seasons'))
        episodes = _ints(parsed.get('episodes'))
        positions: Set[Tuple[int, int]] = set()
        if explicit_absolute:
            pos = self._by_number(explicit_absolute)
            return {pos} if pos else positions
        if not episodes and not seasons and bare_number:
            episodes = [bare_number]
        if not episodes:
            return positions
        if not seasons:
            # "Show - 030": the number is already absolute
            for n in set(episodes):
                pos = self._by_number([n])
                if not pos:
                    return set()
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
                        pos = self._by_number(self.alt_abs[(s, e)], from_name=False)
                elif (s, e) in self.show_abs:
                    pos = (s, e)
                elif s == 1:
                    # "S01E30" past the end of season 1: every episode under S01
                    pos = self._by_number([e])
                if pos:
                    positions.add(pos)
        return positions


def _special_slots(seasons: Optional[Dict[Any, Any]],
                   provider_claims: Dict[int, Set[Tuple[int, int]]]) -> Set[int]:
    """Numbers the provider's absolute numbering skips for a special.

    TVDB's One Piece goes S16E11 = 589, S16E12 = 591: 590 is the Dream 9
    crossover, filed as S00E39 and aired between the two. A skipped number
    counts when a special aired between its neighbours (dates inclusive).
    """
    specials = (seasons or {}).get(0) or (seasons or {}).get('0') or {}
    special_dates = sorted(str(ep.get('first_aired'))[:10]
                           for ep in ((specials.get('episodes') or {}).values() if isinstance(specials, dict) else [])
                           if isinstance(ep, dict) and ep.get('first_aired'))
    if not special_dates or not provider_claims:
        return set()

    def aired(number):
        positions = provider_claims.get(number) or set()
        if len(positions) != 1:
            return None
        s_num, e_num = next(iter(positions))
        s_data = seasons.get(s_num) or seasons.get(str(s_num)) or {}
        episodes = s_data.get('episodes') or {}
        ep = episodes.get(e_num) or episodes.get(str(e_num)) or {}
        return str(ep.get('first_aired'))[:10] if ep.get('first_aired') else None

    slots = set()
    numbered = sorted(provider_claims)
    for before, after in zip(numbered, numbered[1:]):
        if after - before < 2:
            continue
        start, end = aired(before), aired(after)
        if start and end and any(start <= d <= end for d in special_dates):
            slots.update(range(before + 1, after))
    return slots


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
    A multi-episode file ("001-002") is assigned to each episode it holds,
    also when the S/E pass already gave it to the first one.
    Returns how many items were assigned.
    """
    numbering = _ShowNumbering(show_seasons, alt_seasons)
    bare_numbers = pack_bare_numbers(parsed_files)

    items_by_pos: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for item in target_items:
        if item.get('type') != 'episode':
            continue
        pos = (item.get('season_number'), item.get('episode_number'))
        numbers = numbering.show_abs.get(pos) or []
        item['absolute_number'] = numbers[0] if numbers else None
        if not item.get('assigned'):
            items_by_pos[pos] = item

    candidates_by_file = []
    for f in parsed_files:
        multi_episode = len(set(_ints((f.get('parsed') or {}).get('episodes')))) > 1
        if f.get('used') and not multi_episode:
            continue
        filename = (f.get('original') or {}).get('filename')
        if is_anime_op_ed_extra(filename) or is_anime_op_ed_extra((f.get('original') or {}).get('path')):
            continue
        explicit = file_explicit_absolute(f.get('parsed') or {}, filename, show_seasons)
        candidates_by_file.append((f, multi_episode, explicit))

    # Survey the whole pack first: which absolute numbering it follows
    # decides numbers the provider's numbering and a plain count both claim
    numbering.quiet = True
    for f, _, explicit in candidates_by_file:
        numbering.file_positions(f.get('parsed') or {}, explicit, bare_numbers.get(id(f)))
    numbering.quiet = False
    numbering.prefer_numbering()

    files_by_pos: Dict[Tuple[int, int], List[Dict[str, Any]]] = defaultdict(list)
    for f, multi_episode, explicit in candidates_by_file:
        positions = numbering.file_positions(f.get('parsed') or {}, explicit, bare_numbers.get(id(f)))
        if len(positions) > 1 and not multi_episode:
            continue  # one number, several episodes: left to the user
        for pos in positions & set(items_by_pos):
            files_by_pos[pos].append(f)

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
        # A multi-episode "S02E01-E02" file reaches the second episode here by its S/E
        parsed = candidates[0].get('parsed') or {}
        by_se = pos[0] in _ints(parsed.get('seasons') or []) and pos[1] in _ints(parsed.get('episodes') or [])
        item['match_reason'] = 'se' if by_se else 'absolute'
        candidates[0]['used'] = True
        assigned += 1
        logging.info(f"[Absolute] Assigned '{candidates[0]['original'].get('path')}' to "
                     f"{item.get('item_key')} (absolute {item.get('absolute_number')})")
    return assigned
