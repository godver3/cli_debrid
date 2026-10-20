"""
Utilities for handling anime-specific numbering and format detection.
"""
import logging
import re
import threading
import time
import unicodedata
from typing import Dict, Any, List, Optional, Tuple
# from utilities.anidb_functions import get_anidb_metadata_for_item  # Disabled for now

# Known anime that use absolute episode numbering
ABSOLUTE_NUMBERED_ANIME = {
    'one piece',
    'detective conan',
    'case closed',
    'naruto shippuden',  # Uses mixed numbering
    'boruto',
    'black clover',
    'fairy tail',
    'bleach',
    'dragon ball super',
    'pokemon'
}

def is_latin_script(text: str) -> bool:
    """True if every letter in text is a Latin letter (accents and macrons
    included) and it has at least one. Digits, spaces and punctuation are fine:
    romaji titles like "Mob Psycho 100" or "Kimetsu no Yaiba: Yuukaku-hen"
    qualify, kana and kanji don't."""
    has_letter = False
    for ch in text or '':
        if ch.isalpha():
            has_letter = True
            if not unicodedata.name(ch, '').startswith('LATIN'):
                return False
    return has_letter


def _alias_key(text: str) -> str:
    """Lowercase words of an alias, punctuation dropped ("Bleach: Sennen
    Kessen-hen" -> "bleach sennen kessen hen")."""
    return ' '.join(re.sub(r'[\W_]+', ' ', (text or '').lower()).split())


def pick_romanized_alias(aliases: List[str], title: str, skip: Optional[set] = None) -> Optional[str]:
    """The Japan-sourced alias worth searching as the show's romaji name, or None.

    - Kana/kanji forms are skipped (is_latin_script), as is the title itself.
    - Arc and sequel names are skipped: an alias that is another alias (or the
      title) plus more words ("Bleach: Sennen Kessen-hen", "Shingeki no Kyojin
      The Final Season"), and any alias ending in such an arc name
      ("Burīchi: Sennen Kessen-hen"). Searching an arc name for the whole show
      finds the wrong episodes; later seasons get their own name from AniList.
    - Plain ASCII spellings come first: release groups write "Sousou no
      Frieren", not "Sōsō no Furīren", which the provider may list first.
    """
    skip = {s.lower() for s in (skip or ())}
    latin = [a for a in aliases or [] if isinstance(a, str) and a.strip() and is_latin_script(a)]
    bases = {_alias_key(title)} | {_alias_key(a) for a in latin}
    bases.discard('')
    arc_tails = set()
    for a in latin:
        key = _alias_key(a)
        for base in bases:
            if base != key and key.startswith(base + ' '):
                arc_tails.add(key[len(base) + 1:])

    def is_arc_name(alias: str) -> bool:
        key = _alias_key(alias)
        return any(key == tail or key.endswith(' ' + tail) for tail in arc_tails) or any(
            base != key and key.startswith(base + ' ') for base in bases)

    candidates = [a for a in latin
                  if a.lower() not in skip and a.lower() != (title or '').lower() and not is_arc_name(a)]
    for alias in candidates:
        if alias.isascii():
            return alias
    return candidates[0] if candidates else None


def stored_absolute_episode(imdb_id: str, season: int, episode: int) -> Optional[int]:
    """The provider's absolute number for S/E (TVDB absoluteNumber or Trakt
    number_abs), as stored by cli_battery. None if unknown."""
    try:
        from cli_battery.app.database import Session, Item, Season, Episode

        with Session() as session:
            item = session.query(Item).filter_by(imdb_id=imdb_id).first()
            if not item:
                logging.debug(f"Item not found in database for IMDB ID: {imdb_id}")
                return None
            season_obj = session.query(Season).filter_by(item_id=item.id, season_number=season).first()
            if not season_obj:
                logging.debug(f"Season {season} not found for item {imdb_id}")
                return None
            episode_obj = session.query(Episode).filter_by(season_id=season_obj.id, episode_number=episode).first()
            if not episode_obj:
                logging.debug(f"Episode S{season}E{episode} not found for item {imdb_id}")
                return None
            if episode_obj.absolute_episode is not None:
                logging.info(f"Found absolute episode number {episode_obj.absolute_episode} for S{season}E{episode} in database")
                return episode_obj.absolute_episode
            logging.debug(f"Episode S{season}E{episode} exists but has no absolute episode number")
            return None
    except Exception as e:
        logging.warning(f"Error getting absolute episode from database: {e}")
        return None


def season_counts_from_show_seasons(show_seasons: Optional[Dict[Any, Any]]) -> Dict[int, int]:
    """{season: episode_count} from a cli_battery show metadata 'seasons' dict."""
    counts: Dict[int, int] = {}
    for s_num, s_data in (show_seasons or {}).items():
        if isinstance(s_num, int) and isinstance(s_data, dict):
            counts[s_num] = s_data.get('episode_count') or 0
    return counts


def absolute_from_counts(season: Optional[int], episode: Optional[int],
                         season_episode_counts: Optional[Dict[Any, int]]) -> Optional[int]:
    """Absolute episode number by arithmetic: the episodes of every regular
    season before `season`, plus `episode`.

    Season 0 (specials) is never counted: release groups' absolute numbering
    doesn't include specials, and cli_battery stores them, so counting them
    shifted every later season by the number of specials.
    """
    if season is None or episode is None or not season_episode_counts:
        return None
    before = sum(count or 0 for s_num, count in season_episode_counts.items()
                 if isinstance(s_num, int) and 0 < s_num < season)
    return before + episode


# Per-result scrape filtering asks for the same few episodes many times.
_STORED_ABSOLUTE_TTL = 300
_stored_absolute_cache: Dict[Tuple[str, int, int], Tuple[float, Optional[int]]] = {}
_stored_absolute_lock = threading.Lock()


def cached_stored_absolute_episode(imdb_id: str, season: int, episode: int) -> Optional[int]:
    """stored_absolute_episode, remembered for a few minutes."""
    key = (imdb_id, season, episode)
    now = time.monotonic()
    with _stored_absolute_lock:
        hit = _stored_absolute_cache.get(key)
        if hit and now - hit[0] < _STORED_ABSOLUTE_TTL:
            return hit[1]
    value = stored_absolute_episode(imdb_id, season, episode)
    with _stored_absolute_lock:
        if len(_stored_absolute_cache) > 5000:
            _stored_absolute_cache.clear()
        _stored_absolute_cache[key] = (now, value)
    return value


def absolute_episode_for(season: Optional[int], episode: Optional[int],
                         season_episode_counts: Optional[Dict[Any, int]],
                         imdb_id: Optional[str] = None) -> Optional[int]:
    """Absolute episode number for S/E.

    Pass imdb_id only when season/episode are the show's own (metadata)
    numbering: the provider's stored absolute is then preferred. For
    XEM/scene-mapped numbers leave it out, since the stored value is keyed
    by the metadata numbering and would belong to a different episode.
    """
    if imdb_id and season is not None and episode is not None:
        stored = cached_stored_absolute_episode(imdb_id, season, episode)
        if stored is not None:
            return stored
    return absolute_from_counts(season, episode, season_episode_counts)


def xem_absolute(xem_mapping: Optional[list], season: int, episode: int) -> Optional[int]:
    """TVDB (or, failing that, scene) absolute for S/E from an XEM mapping."""
    for mapping_entry in xem_mapping or []:
        tvdb_info = mapping_entry.get('tvdb', {})
        scene_info = mapping_entry.get('scene', {})
        if tvdb_info.get('season') == season and tvdb_info.get('episode') == episode:
            if tvdb_info.get('absolute'):
                logging.info(f"Found XEM tvdb absolute: {tvdb_info.get('absolute')} for S{season}E{episode}")
                return tvdb_info.get('absolute')
            if scene_info and scene_info.get('absolute'):
                logging.info(f"Found XEM scene absolute (fallback): {scene_info.get('absolute')} for S{season}E{episode}")
                return scene_info.get('absolute')
            return None
    return None


def _show_season_counts(imdb_id: str) -> Dict[int, int]:
    """{season: episode_count} for the whole show from cli_battery."""
    from cli_battery.app.direct_api import DirectAPI
    show_metadata, _ = DirectAPI.get_show_metadata(imdb_id)
    if show_metadata and isinstance(show_metadata, dict):
        return season_counts_from_show_seasons(show_metadata.get('seasons'))
    return {}


def resolve_absolute_episode(season: int, episode: int, season_episode_counts: Optional[Dict[Any, int]],
                             xem_mapping: Optional[list] = None, imdb_id: Optional[str] = None) -> int:
    """Absolute episode for the show's own S/E, for building search formats.

    Order: the provider's stored absolute, then XEM, then arithmetic over
    earlier seasons (specials excluded). For season 2+ the arithmetic uses the
    show's full season list from cli_battery, since season_episode_counts only
    covers episodes that are in the library. Never returns None: with no data
    at all the episode number itself is used, as before.
    """
    absolute = stored_absolute_episode(imdb_id, season, episode) if imdb_id else None
    if absolute is not None:
        logging.info(f"Using absolute episode number {absolute} from database for S{season}E{episode}")
        return absolute
    if xem_mapping:
        absolute = xem_absolute(xem_mapping, season, episode)
        if absolute is not None:
            return absolute
    logging.info("No stored or XEM absolute, using arithmetic calculation")
    if season > 1 and imdb_id:
        try:
            absolute = absolute_from_counts(season, episode, _show_season_counts(imdb_id))
            if absolute is not None:
                logging.info(f"DirectAPI calculated absolute episode number: {absolute}")
                return absolute
            logging.warning(f"DirectAPI returned no season data for {imdb_id}, falling back to arithmetic")
        except Exception as api_err:
            logging.warning(f"Failed to get season counts from DirectAPI for season {season}: {api_err}")
    absolute = absolute_from_counts(season, episode, season_episode_counts)
    if absolute is None:
        absolute = episode
    logging.info(f"Arithmetic calculated absolute episode number: {absolute}")
    return absolute


def full_season_counts(imdb_id: Optional[str], season_episode_counts: Optional[Dict[Any, int]]) -> Dict[int, int]:
    """{season: episode_count} for every regular season of the show.
    season_episode_counts may only cover the library; cli_battery's season list
    is preferred when there's an imdb_id."""
    counts: Dict[int, int] = {}
    if imdb_id:
        try:
            counts = _show_season_counts(imdb_id)
        except Exception as e:
            logging.debug(f"Could not load season counts for {imdb_id}: {e}")
    if not counts:
        counts = {s: c for s, c in (season_episode_counts or {}).items() if isinstance(s, int)}
    return {s: c for s, c in counts.items() if s > 0 and c}


def seasons_in_absolute_batch(episodes, season_counts: Dict[int, int], imdb_id: Optional[str] = None) -> List[int]:
    """Regular seasons all of whose episodes are in a batch numbered by absolute
    episode ("[Erai-raws] Bleach - 001 ~ 366 [BATCH]" -> seasons 1-16)."""
    numbers = set(episodes or [])
    covered = []
    for s_num in sorted(season_counts):
        first = absolute_episode_for(s_num, 1, season_counts, imdb_id)
        last = absolute_episode_for(s_num, season_counts[s_num], season_counts, imdb_id)
        if first and last and last >= first and all(n in numbers for n in range(first, last + 1)):
            covered.append(s_num)
    return covered


# A bracketed number or range next to a season: "S13E01 (0422)", "Season 13 [422-458]"
_EXPLICIT_ABS_RANGE = re.compile(r'[(\[]\s*(\d{2,4})\s*(?:-|~|to)\s*(\d{2,4})\s*[)\]]', re.IGNORECASE)
_EXPLICIT_ABS_SINGLE = re.compile(r'[(\[]\s*(\d{2,4})\s*[)\]]')
_NAMED_SEASON = re.compile(r'(?<![a-z0-9])(?:s\d{1,2}(?![0-9])|season\s*\d{1,2}(?![0-9]))', re.IGNORECASE)
_RESOLUTIONS = {360, 480, 540, 576, 720, 1080, 1440, 2160, 4320}


def _plausible_absolute(text: str) -> bool:
    n = int(text)
    if n in _RESOLUTIONS:
        return False
    # A year, unless zero-padded ("(0422)")
    return not (len(text) == 4 and not text.startswith('0') and 1950 <= n <= 2099)


def explicit_absolute_numbers(name: str, seasons=None, episodes=None,
                              season_counts: Optional[Dict[int, int]] = None) -> List[int]:
    """Absolute episode numbers a release name states next to its own season.

    Some releases carry both numberings: "One Piece S13E01 (0422)" or a pack
    "One Piece Season 13 - Impel Down [422-458]". The season there is the
    releaser's own split, which often matches none of the show's orders, while
    the absolute number means the same episode in every order. Names with no
    season are left alone: their number already is the absolute one.

    Not taken as absolute: a number no larger than the name's own episode
    ("S02E05 (05)"), a range starting at 1 ("S02 [01-12]" counts the season),
    years and resolutions. With season_counts (the show's {season: episodes}),
    numbers that fit inside the named season ("S03 Part 2 [13-22]" for a
    22-episode season 3) count that season, not the show.
    """
    if not name or not _NAMED_SEASON.search(name):
        return []
    seasons = [s for s in (seasons or []) if isinstance(s, int)]
    if not seasons or max(seasons) < 1:
        return []
    episodes = [e for e in (episodes or []) if isinstance(e, int)]
    season_size = max((season_counts or {}).get(s, 0) or 0 for s in seasons)

    def beyond_season(numbers: List[int]) -> bool:
        return not season_counts or max(numbers) > season_size

    for match in _EXPLICIT_ABS_RANGE.finditer(name):
        lo_text, hi_text = match.groups()
        if not (_plausible_absolute(lo_text) and _plausible_absolute(hi_text)):
            continue
        lo, hi = int(lo_text), int(hi_text)
        if 1 < lo < hi and hi - lo < 2000 and beyond_season([hi]):
            return list(range(lo, hi + 1))
    # A single number only counts next to an episode it is larger than
    if len(episodes) != 1:
        return []
    for match in _EXPLICIT_ABS_SINGLE.finditer(name):
        text = match.group(1)
        if _plausible_absolute(text) and int(text) > episodes[0] and beyond_season([int(text)]):
            return [int(text)]
    return []


def detect_absolute_numbering(title: str, season: int, episode: int, tmdb_id: Optional[str] = None) -> Tuple[bool, Optional[int]]:
    """
    Detect if an anime uses absolute episode numbering.
    
    Returns:
        Tuple of (uses_absolute_numbering: bool, absolute_episode: Optional[int])
    """
    title_lower = title.lower().strip()
    
    # Check if it's a known absolute-numbered anime
    for known_anime in ABSOLUTE_NUMBERED_ANIME:
        if known_anime in title_lower:
            logging.info(f"Detected known absolute-numbered anime: {title}")
            # For these anime, if episode number is > 100, it's likely already absolute
            if episode > 100:
                return True, episode
            # Otherwise, might need more complex calculation
            break
    
    # Heuristic: If episode number is unusually high (> 100) for the given season
    # it's likely already an absolute episode number
    if episode > 100:
        logging.info(f"Episode number {episode} is > 100, likely absolute numbering")
        return True, episode
    
    # For anime with high season numbers (> 10) and high episode numbers,
    # this might indicate misinterpreted absolute numbering
    if season > 10 and episode > 50:
        logging.info(f"High season ({season}) and episode ({episode}) numbers suggest absolute numbering")
        # The episode number is likely the absolute episode
        return True, episode
    
    return False, None


def get_correct_anime_episode_info(title: str, season: int, episode: int, 
                                  tmdb_id: Optional[str] = None,
                                  imdb_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Get correct episode information for anime, handling absolute numbering.
    
    Uses heuristics and known anime list to detect absolute numbering.
    Jikan API integration is currently disabled.
    
    Returns a dict with:
        - uses_absolute: bool
        - absolute_episode: int
        - season: int (corrected if needed)
        - episode: int (corrected if needed)
        - formats: Dict[str, str] (episode format strings)
    """
    result = {
        'uses_absolute': False,
        'absolute_episode': None,
        'season': season,
        'episode': episode,
        'formats': {}
    }
    
    # First, try to detect if this anime uses absolute numbering
    uses_absolute, detected_absolute = detect_absolute_numbering(title, season, episode, tmdb_id)
    
    if uses_absolute:
        result['uses_absolute'] = True
        result['absolute_episode'] = detected_absolute
        
        # Jikan API integration disabled for now
        # TODO: Re-enable when needed with proper error handling
        
        # For absolute numbered anime, the formats should use the absolute number
        abs_ep = result['absolute_episode']
        
        # Determine padding
        padding = 4 if abs_ep > 999 else 3
        
        result['formats'] = {
            'no_zeros': str(abs_ep),
            'regular': f"E{abs_ep}",  # Just E + absolute number for these anime
            'absolute_with_e': f"E{abs_ep:0{padding}d}",
            'absolute': f"{abs_ep:0{padding}d}",
            'combined': f"Episode {abs_ep}",  # Common format for absolute numbered anime
            'absolute_no_padding': str(abs_ep)  # Always include unpadded version
        }
    
    return result


def convert_anime_episode_format_smart(season: int, episode: int, 
                                       season_episode_counts: Dict[int, int],
                                       title: Optional[str] = None,
                                       tmdb_id: Optional[str] = None,
                                       imdb_id: Optional[str] = None) -> Dict[str, str]:
    """
    Smart anime episode format conversion that handles absolute numbering.
    
    This is a wrapper around the original convert_anime_episode_format that adds
    detection for absolute-numbered anime.
    """
    # If we have title information, check if this anime uses absolute numbering
    if title:
        anime_info = get_correct_anime_episode_info(title, season, episode, tmdb_id, imdb_id)
        
        if anime_info['uses_absolute']:
            logging.info(f"Using absolute numbering formats for {title}")
            return anime_info['formats']
    
    # Otherwise, fall back to the original calculation
    # This will do the traditional season-based absolute calculation
    from scraper.scraper import convert_anime_episode_format
    return convert_anime_episode_format(season, episode, season_episode_counts)
