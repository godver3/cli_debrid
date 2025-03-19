"""Common utility functions shared across the scraper module."""
import re
import logging
from typing import Dict, Any, Union, List, Optional
from PTT import parse_title
from scraper.functions.ptt_parser import parse_with_ptt

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
    if isinstance(parsed_info, str):
        logging.debug(f"Parsing string with PTT for season/episode detection: '{parsed_info}'")
        # Parse string using PTT
        parsed_info = parse_with_ptt(parsed_info)
        logging.debug(f"PTT parse result: {parsed_info}")
    else:
        logging.debug(f"Using pre-parsed info for season/episode detection: {parsed_info.get('title', 'no_title')}")
    
    result = {
        'season_pack': 'Unknown',
        'multi_episode': False,
        'seasons': [],
        'episodes': []
    }
    
    # Check for complete series indicators
    title = parsed_info.get('title', '').lower()
    original_title = parsed_info.get('original_title', '').lower()
    
    if any(indicator in title for indicator in ['complete', 'collection', 'all.seasons']) or \
       any(indicator in original_title for indicator in ['complete', 'collection', 'all.seasons']):
        logging.debug(f"Complete series detected via indicators in title")
        result['season_pack'] = 'Complete'
        return result

    # Get season and episode info from parsed info
    season_info = parsed_info.get('seasons', [])
    episode_info = parsed_info.get('episodes', [])
    
    logging.debug(f"Raw season info: {season_info}")
    logging.debug(f"Raw episode info: {episode_info}")
    
    # Handle season information
    if season_info:
        if isinstance(season_info, list) and len(season_info) > 1:
            # If we have multiple seasons, it's a season pack
            result['season_pack'] = ','.join(str(s) for s in sorted(set(season_info)))
            result['seasons'] = sorted(set(season_info))
        else:
            # Single season - check if it's a pack or single episode
            season_value = season_info[0] if isinstance(season_info, list) else season_info
            if not episode_info:
                # No episode number means it's a season pack
                result['season_pack'] = str(season_value)
            else:
                result['season_pack'] = 'N/A'  # Single episode
            result['seasons'] = [season_value]
    else:
        # No season info
        if episode_info:
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
    if episode_info:
        if isinstance(episode_info, list) and len(episode_info) > 1:
            result['multi_episode'] = True
            result['episodes'] = sorted(set(episode_info))
        else:
            episode_value = episode_info[0] if isinstance(episode_info, list) else episode_info
            result['episodes'] = [episode_value]
            if not result['seasons']:
                result['seasons'] = [1]
    
    return result
