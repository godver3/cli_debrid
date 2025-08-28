"""
Utilities for handling anime-specific numbering and format detection.
"""
import logging
import re
from typing import Dict, Any, Optional, Tuple
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
