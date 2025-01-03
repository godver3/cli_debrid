"""Common utility functions shared across the scraper module."""
import re
import logging
from typing import Dict, Any, Union, List, Optional
from guessit import guessit

def trim_magnet(magnet: str):
    """Remove unnecessary parts from magnet link."""
    if '&amp;' in magnet:
        magnet = magnet.split('&amp;')[0]
    return magnet.split('&tr=')[0]

def round_size(size: str):
    """Round file size to 2 decimal places."""
    try:
        return round(float(size), 2)
    except (ValueError, TypeError):
        return 0.0

def detect_season_episode_info(parsed_info: Union[Dict[str, Any], str]) -> Dict[str, Any]:
    """
    Detect season and episode information from parsed torrent info.
    Returns a dictionary containing season pack info, multi-episode flag, and lists of seasons/episodes.
    """
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

    # Check for complete series indicators
    title = parsed_info.get('title', '').lower()
    if any(indicator in title for indicator in ['complete', 'collection', 'all.seasons']):
        result['season_pack'] = 'Complete'
        return result

    # Get season info - PTT uses 'seasons', guessit uses 'season'
    season_info = parsed_info.get('seasons') or parsed_info.get('season')
    episode_info = parsed_info.get('episodes') or parsed_info.get('episode')
    
    # Handle season information
    if season_info is not None:
        if isinstance(season_info, list):
            # If we have multiple seasons, it's a season pack
            result['season_pack'] = ','.join(str(s) for s in sorted(set(season_info)))
            result['seasons'] = sorted(set(season_info))
        else:
            # Single season - check if it's a pack or single episode
            if episode_info is None:
                # No episode number means it's a season pack
                result['season_pack'] = str(season_info)
            else:
                result['season_pack'] = 'N/A'  # Single episode
            result['seasons'] = [season_info]
    else:
        # No season info
        if episode_info is not None:
            # Has episode but no season - assume season 1
            result['season_pack'] = 'N/A'
            result['seasons'] = [1]
        else:
            # No season or episode info - might be a complete pack
            if any(word in title.lower() for word in ['season', 'complete', 'collection']):
                result['season_pack'] = 'Complete'
            else:
                result['season_pack'] = 'Unknown'
    
    # Handle episode information
    if episode_info is not None:
        if isinstance(episode_info, list):
            result['multi_episode'] = True
            result['episodes'] = sorted(set(episode_info))
        else:
            result['episodes'] = [episode_info]
            if not result['seasons']:
                result['seasons'] = [1]
    
    #logging.debug(f"Season/episode detection for title '{title}': {result}")
    return result
