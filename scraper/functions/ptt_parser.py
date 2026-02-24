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
        result = parse_title(title)

        
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
            'country': result.get('country')  # Include country code from PTT
        }

        
        # Handle single season/episode for compatibility
        if len(processed['seasons']) == 1:
            processed['season'] = processed['seasons'][0]
        if len(processed['episodes']) == 1:
            processed['episode'] = processed['episodes'][0]
            
        return processed
    except Exception as e:
        logging.error(f"Error parsing title with PTT: {str(e)}")
        return {
            'title': title,
            'original_title': title,
            'parsing_error': True
        }
