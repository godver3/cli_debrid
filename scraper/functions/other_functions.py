import logging
import re
from typing import Dict, Any, Tuple, Optional, Union
import pykakasi
from scraper.functions.common import round_size, trim_magnet
from PTT import parse_title

def romanize_japanese(text):
    kks = pykakasi.kakasi()
    result = kks.convert(text)
    return ' '.join([item['hepburn'] for item in result])

def validate_regex(pattern: str) -> Tuple[bool, Optional[str]]:
    """
    Validates if a pattern is a valid regex and returns any error message.
    
    Args:
        pattern (str): The regex pattern to validate.
    
    Returns:
        Tuple[bool, Optional[str]]: (is_valid, error_message)
    """
    try:
        re.compile(pattern)
        return True, None
    except re.error as e:
        return False, str(e)

def is_regex(pattern):
    """Check if a pattern is likely to be a regex."""
    return any(char in pattern for char in r'.*?+^$()[]{}|\\') and not (pattern.startswith('"') and pattern.endswith('"'))

def smart_search(pattern, text):
    """Perform either regex search or simple string matching."""
    if pattern.startswith('"') and pattern.endswith('"'):
        # Remove quotes and perform case-insensitive substring search
        return pattern[1:-1].lower() in text.lower()
    elif is_regex(pattern):
        is_valid, error = validate_regex(pattern)
        if not is_valid:
            logging.error(f"Invalid regex pattern '{pattern}': {error}")
            # Fall back to simple string matching
            return pattern.lower() in text.lower()
        try:
            return re.search(pattern, text, re.IGNORECASE) is not None
        except re.error as e:
            logging.error(f"Error applying regex pattern '{pattern}': {str(e)}")
            # Fall back to simple string matching
            return pattern.lower() in text.lower()
    else:
        return pattern.lower() in text.lower()


def detect_season_episode_info(parsed_info: Union[Dict[str, Any], str]) -> Dict[str, Any]:
    result = {
        'season_pack': 'Unknown',
        'multi_episode': False,
        'seasons': [],
        'episodes': []
    }

    if isinstance(parsed_info, str):
        try:
            parsed_info = parse_title(parsed_info)
        except Exception as e:
            logging.error(f"Error parsing title with parse_title: {str(e)}")
            return result

    # Extract season information from parse_title format
    seasons = parsed_info.get('seasons', [])
    episodes = parsed_info.get('episodes', [])
    complete = parsed_info.get('complete', False)
    
    # Handle season information
    if seasons:
        if len(seasons) > 1:
            result['season_pack'] = ','.join(str(s) for s in sorted(set(seasons)))
            result['seasons'] = sorted(set(seasons))
        else:
            result['season_pack'] = str(seasons[0])
            result['seasons'] = seasons
        
        # Mark as season pack if complete is True or no episodes specified
        if complete or (not episodes and seasons):
            result['season_pack'] = ','.join(str(s) for s in sorted(set(seasons)))
    else:
        # Assume season 1 if no season is detected but episode is present
        if episodes:
            result['season_pack'] = '1'
            result['seasons'] = [1]
    
    # Handle episode information
    if episodes:
        if len(episodes) > 1:
            result['multi_episode'] = True
            result['episodes'] = episodes
        else:
            result['episodes'] = episodes
            if not result['seasons']:  # If seasons is still empty, assume season 1
                result['seasons'] = [1]
            result['season_pack'] = 'N/A'  # Indicate it's a single episode, not a pack
    
    return result

