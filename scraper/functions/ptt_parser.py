"""
Shared PTT parsing functionality for consistent parsing across the application.
"""
import logging
from typing import Dict, Any
from functools import lru_cache
from PTT import parse_title
import re

# Pre-compiled patterns for Spanish Cap. format conversion
_CAP_RANGE_RE = re.compile(r'\bCap\.(\d{3,4})_(\d{3,4})\b', re.IGNORECASE)
_CAP_SINGLE_RE = re.compile(r'\bCap\.(\d{3,4})\b', re.IGNORECASE)

# Pattern to extract a parenthesized alternative title near the start of a filename.
# Matches: "El renacido (The Revenant) 2015 ..." → "The Revenant"
# Only matches parentheses within the first ~60 chars to avoid matching years/tags like (2015) or [4K]
_PAREN_ALT_TITLE_RE = re.compile(r'^.{0,60}\(([A-Za-z][^)]{2,})\)', re.IGNORECASE)


def extract_parenthesized_title(title: str) -> str | None:
    """Extract an alternative title in parentheses near the start of the filename.
    e.g. 'El renacido (The Revenant) 2015 SPANISH' → 'The Revenant'
    Returns None if no such pattern is found or the match looks like a year/tag.
    """
    m = _PAREN_ALT_TITLE_RE.match(title)
    if not m:
        return None
    candidate = m.group(1).strip()
    # Reject if it looks like a year (4 digits) or a short tag
    if re.fullmatch(r'\d{4}', candidate):
        return None
    return candidate


# A release name wrapped whole in brackets, e.g. "[ Show.S01E05.1080p.WEB-DL-GRP ] -" (seen
# from NinjaCentral). PTT returns an empty title for these.
_WRAPPED_RELEASE_RE = re.compile(r'^\s*[\[(]\s*(?P<inner>[^\[\]()]+?)\s*[\])][\s._-]*$')
# Where the title part of a release name ends: season/episode, year or resolution.
_TITLE_END_RE = re.compile(
    r'(?i)(?:^|[\s._-])(?:s\d{1,2}(?:[\s._-]*e\d{1,3})?|season[\s._-]*\d|\d{1,2}x\d{1,3}|(?:19|20)\d{2}|\d{3,4}p)'
    r'(?=$|[\s._\-\])])'
)


def unwrap_release_title(title: str) -> str | None:
    """Inner name of a release wrapped whole in brackets, or None if it isn't wrapped."""
    m = _WRAPPED_RELEASE_RE.match(title or '')
    return m.group('inner') if m else None


def parse_title_unwrapped(title: str, parse=parse_title) -> Dict[str, Any]:
    """PTT parse that retries without the wrapper when a bracket-wrapped name yields no title."""
    result = parse(title)
    if not result.get('title'):
        inner = unwrap_release_title(title)
        if inner:
            unwrapped = parse(inner)
            if unwrapped.get('title'):
                return unwrapped
    return result


def title_before_markers(title: str) -> str:
    """Title part of a release name PTT couldn't title.

    Unwraps the name, drops leading [tags] and cuts at the first season/episode, year or
    resolution marker. Returns '' when there is no marker to cut at.
    """
    name = unwrap_release_title(title) or title or ''
    name = re.sub(r'^(?:\s*\[[^\]]*\])+\s*', '', name)
    m = _TITLE_END_RE.search(name)
    if not m:
        return ''
    return re.sub(r'[\s._\-\[\]()]+', ' ', name[:m.start()]).strip()


def _cap_digits_to_season_episode(digits: str) -> tuple:
    """Split 3-4 digit Cap. number into (season, episode).
    Last 2 digits = episode, preceding digits = season.
    e.g. '701' → (7, 1), '1905' → (19, 5)
    """
    episode = int(digits[-2:])
    season = int(digits[:-2]) if len(digits) > 2 else 0
    return season, episode


def convert_spanish_cap_format(title: str) -> str:
    """
    Convert Spanish Cap.XXYY episode notation to standard SxxExx format
    so PTT can correctly parse season and episode numbers.

    Examples:
      "Show - Cap.701"       → "Show - S07E01"
      "Show [Cap.1905]"      → "Show [S19E05]"
      "Show [Cap.701_708]"   → "Show [S07E01E08]"
    """
    def replace_range(m):
        s1, e1 = _cap_digits_to_season_episode(m.group(1))
        s2, e2 = _cap_digits_to_season_episode(m.group(2))
        # Both parts should be same season; use s1
        return f"S{s1:02d}E{e1:02d}E{e2:02d}"

    def replace_single(m):
        s, e = _cap_digits_to_season_episode(m.group(1))
        return f"S{s:02d}E{e:02d}"

    # Replace ranges first, then singles
    title = _CAP_RANGE_RE.sub(replace_range, title)
    title = _CAP_SINGLE_RE.sub(replace_single, title)
    return title

@lru_cache(maxsize=1024)
def parse_with_ptt(title: str) -> Dict[str, Any]:
    """
    Parse a title using PTT with caching.
    Returns a standardized format that can be used across the application.
    """
    try:
        # Get the raw result from PTT
        result = parse_title_unwrapped(title)

        
        # Convert to our standard format
        processed = {
            'title': result.get('title'),
            'original_title': result.get('original_title'),
            'type': 'movie' if not result.get('seasons') and not result.get('episodes') else 'episode',
            'year': result.get('year'),
            'resolution': result.get('resolution', 'Unknown'),
            'source': result.get('source'),
            'audio': result.get('audio'),
            'codec': result.get('codec'),
            'group': result.get('group'),
            'seasons': result.get('seasons', []),
            'episodes': result.get('episodes', []),
            'site': result.get('site'),  # Store the site separately
            'trash': result.get('trash', False),  # Include trash flag
            'country': result.get('country'),  # Include country code from PTT
            'languages': result.get('languages', [])  # ISO 639-1 audio/sub language codes from PTT
        }

        
        # Handle single season/episode for compatibility
        if len(processed['seasons']) == 1:
            processed['season'] = processed['seasons'][0]
        if len(processed['episodes']) == 1:
            processed['episode'] = processed['episodes'][0]

        # Extract parenthesized alternative title (e.g. Spanish releases: "El renacido (The Revenant)")
        paren_title = extract_parenthesized_title(title)
        if paren_title:
            processed['parenthesized_title'] = paren_title

        return processed
    except Exception as e:
        logging.error(f"Error parsing title with PTT: {str(e)}")
        return {
            'title': title,
            'original_title': title,
            'parsing_error': True
        }
