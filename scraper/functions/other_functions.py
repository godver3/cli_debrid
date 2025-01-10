import logging
import re
from api_tracker import api
from typing import Dict, Any, Tuple, Optional, Union
from guessit import guessit
import pykakasi
from scraper.functions.common import round_size, trim_magnet

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

def test_regex_patterns():
    """Test function to demonstrate valid and invalid regex patterns."""
    # Valid pattern examples
    valid_pattern = r"S\d{2}E\d{2}"  # Matches patterns like S01E01
    test_text = "Show.S01E02.1080p"
    result = smart_search(valid_pattern, test_text)
    logging.info(f"Valid pattern '{valid_pattern}' on text '{test_text}': {result}")
    
    # Invalid pattern example (unmatched parenthesis)
    invalid_pattern = r"S\d{2}E\d{2})"  # Missing opening parenthesis
    result = smart_search(invalid_pattern, test_text)
    logging.info(f"Invalid pattern '{invalid_pattern}' on text '{test_text}': {result}")

    # Test TS patterns
    # Invalid TS pattern (bad escape at end)
    bad_ts_pattern = r"\.TS.\\"  # This is the problematic pattern
    test_ts_text = "Movie.TS.1080p"
    result = smart_search(bad_ts_pattern, test_ts_text)
    logging.info(f"Bad TS pattern '{bad_ts_pattern}' on text '{test_ts_text}': {result}")
    
    # Correct TS pattern
    good_ts_pattern = r"\.TS\."  # Properly escaped dots
    result = smart_search(good_ts_pattern, test_ts_text)
    logging.info(f"Good TS pattern '{good_ts_pattern}' on text '{test_ts_text}': {result}")

def get_tmdb_season_info(tmdb_id: int, season_number: int, api_key: str) -> Optional[Dict[str, Any]]:
    url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_number}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "accept": "application/json"
    }
    try:
        response = api.get(url, headers=headers)
        response.raise_for_status()
        return response.json()
    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching TMDB season info: {e}")
        return None

def detect_season_episode_info(parsed_info: Union[Dict[str, Any], str]) -> Dict[str, Any]:
    result = {
        'season_pack': 'Unknown',
        'multi_episode': False,
        'seasons': [],
        'episodes': []
    }

    if isinstance(parsed_info, str):
        try:
            parsed_info = guessit(parsed_info)
        except Exception as e:
            logging.error(f"Error parsing title with guessit: {str(e)}")
            return result

    season_info = parsed_info.get('season')
    episode_info = parsed_info.get('episode')
    
    # Handle season information
    if season_info is not None:
        if isinstance(season_info, list):
            result['season_pack'] = ','.join(str(s) for s in sorted(set(season_info)))
            result['seasons'] = sorted(set(season_info))
        else:
            result['season_pack'] = str(season_info)
            result['seasons'] = [season_info]
    else:
        # Assume season 1 if no season is detected but episode is present
        if episode_info is not None:
            result['season_pack'] = '1'
            result['seasons'] = [1]
    
    # Handle episode information
    if episode_info is not None:
        if isinstance(episode_info, list):
            result['multi_episode'] = True
            result['episodes'] = sorted(set(episode_info))
        else:
            result['episodes'] = [episode_info]
            if not result['seasons']:  # If seasons is still empty, assume season 1
                result['seasons'] = [1]
            result['season_pack'] = 'N/A'  # Indicate it's a single episode, not a pack
    
    return result

def extract_season_episode(parsed_info: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    season = parsed_info.get('season')
    episode = parsed_info.get('episode')
    
    # Convert to int if present, otherwise keep as None
    season = int(season) if season is not None else None
    episode = int(episode) if episode is not None else None
    
    return season, episode

def extract_title_and_se(parsed_info: Dict[str, Any]) -> Tuple[str, Optional[int], Optional[int]]:
    title = parsed_info.get('title', '')
    season = parsed_info.get('season')
    episode = parsed_info.get('episode')
    
    # Convert to int if present, otherwise keep as None
    season = int(season) if season is not None else None
    episode = int(episode) if episode is not None else None
    
    return title, season, episode