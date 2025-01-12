import logging
import sys
import os
from typing import Dict, Any, Optional, List
import requests
import time
from datetime import datetime, timedelta

# Add the root directory to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from settings import get_setting

# Jikan API constants
JIKAN_API_URL = "https://api.jikan.moe/v4"
MIN_REQUEST_INTERVAL = timedelta(seconds=1)  # Jikan has a rate limit of 60 requests per minute
last_request_time = datetime.min

# Cache for episode data
_episode_cache: Dict[int, List[Dict[str, Any]]] = {}
_cache_expiry: Dict[int, datetime] = {}
CACHE_DURATION = timedelta(hours=24)  # Cache episode data for 24 hours

def _make_request(endpoint: str, params: Dict[str, Any] = None) -> Optional[Dict[str, Any]]:
    """Make a rate-limited request to Jikan API."""
    global last_request_time
    
    try:
        # Respect rate limiting
        now = datetime.now()
        time_since_last = now - last_request_time
        if time_since_last < MIN_REQUEST_INTERVAL:
            sleep_time = (MIN_REQUEST_INTERVAL - time_since_last).total_seconds()
            time.sleep(sleep_time)
        
        # Make request
        url = f"{JIKAN_API_URL}/{endpoint}"
        logging.debug(f"Making request to {url} with params: {params}")
        response = requests.get(url, params=params, timeout=10)
        last_request_time = datetime.now()
        
        if response.status_code == 200:
            return response.json()
        else:
            logging.error(f"Request failed with status {response.status_code}: {response.text}")
            return None
            
    except Exception as e:
        logging.error(f"Error making request: {str(e)}")
        return None

def _search_anime(title: str) -> Optional[Dict[str, Any]]:
    """Search for an anime by title using Jikan API."""
    try:
        params = {
            'q': title,
            'type': 'tv',  # Focus on TV series
            'limit': 1     # Get only the best match
        }
        
        result = _make_request('anime', params)
        if not result or not result.get('data'):
            return None
            
        # Get the first (best) match
        anime = result['data'][0]
        logging.debug(f"Found anime: {anime.get('title')} (ID: {anime.get('mal_id')})")
        return anime
        
    except Exception as e:
        logging.error(f"Error searching anime: {str(e)}")
        return None

def _get_episode_details(mal_id: int, episode_number: int) -> Optional[Dict[str, Any]]:
    """Get episode details from Jikan API."""
    try:
        now = datetime.now()
        
        # Check if we need to refresh the cache
        if mal_id not in _episode_cache or mal_id not in _cache_expiry or now > _cache_expiry[mal_id]:
            # Get episode list
            result = _make_request(f'anime/{mal_id}/episodes')
            if not result or not result.get('data'):
                return None
                
            # Cache the episodes
            _episode_cache[mal_id] = result['data']
            _cache_expiry[mal_id] = now + CACHE_DURATION
            logging.debug(f"Cached {len(_episode_cache[mal_id])} episodes for anime {mal_id}")
        else:
            logging.debug(f"Using cached episodes for anime {mal_id}")
            
        # Find the specific episode from cache
        for episode in _episode_cache[mal_id]:
            if episode.get('mal_id') == episode_number:
                logging.debug(f"Found episode: {episode.get('title')}")
                return episode
                
        return None
        
    except Exception as e:
        logging.error(f"Error getting episode details: {str(e)}")
        return None

def _get_related_anime(mal_id: int) -> List[Dict[str, Any]]:
    """Get related anime to check for split cours/seasons."""
    try:
        result = _make_request(f'anime/{mal_id}/relations')
        if not result or not result.get('data'):
            return []
            
        # Look for sequels and related series
        related = []
        for relation in result['data']:
            if relation.get('relation') in ['Sequel', 'Alternative version']:
                for entry in relation.get('entry', []):
                    if entry.get('type') == 'anime':
                        related.append(entry)
                        
        return related
        
    except Exception as e:
        logging.error(f"Error getting related anime: {str(e)}")
        return []

def _determine_season_info(anime_data: Dict[str, Any], episode_number: int) -> tuple[int, int]:
    """
    Determine the correct season number and adjusted episode number.
    Returns tuple of (season_number, adjusted_episode_number)
    """
    try:
        # First check explicit season in title
        title_lower = anime_data.get('title', '').lower()
        for i in range(9, 0, -1):  # Check from season 9 down to 1
            if f'season {i}' in title_lower or f' {i}nd season' in title_lower or f' {i}rd season' in title_lower or f' {i}th season' in title_lower:
                return i, episode_number
                
        # Get total episodes for this season
        total_episodes = anime_data.get('episodes', 0)
        if not total_episodes:
            return 1, episode_number
            
        # Check if this might be a split cour series
        if total_episodes in [10, 11, 12, 13, 24, 25, 26]:
            # Get related anime to check for split cours
            related = _get_related_anime(anime_data['mal_id'])
            if related:
                # This might be a split cour series
                if total_episodes <= 13:  # Common length for split cours
                    # If episode number is within this season's range
                    if episode_number <= total_episodes:
                        return 1, episode_number
                    # If episode number is beyond this season
                    else:
                        return 2, episode_number - total_episodes
                elif total_episodes <= 26:  # Two cours combined
                    # Split into two seasons
                    if episode_number <= total_episodes // 2:
                        return 1, episode_number
                    else:
                        return 2, episode_number - (total_episodes // 2)
                        
        # Default case
        return 1, episode_number
        
    except Exception as e:
        logging.error(f"Error determining season info: {str(e)}")
        return 1, episode_number

def get_anidb_metadata_for_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Get anime metadata for a given item using Jikan API."""
    try:
        # Search for the anime
        anime_data = _search_anime(item.get('title', ''))
        if not anime_data:
            return None
            
        # Get episode details if available
        episode_number = int(item.get('episode_number', 0))
        episode_data = None
        if anime_data.get('mal_id'):
            episode_data = _get_episode_details(anime_data['mal_id'], episode_number)
            
        # Extract year from aired date (format: "2020-10-03T00:00:00+00:00")
        year = ''
        if anime_data.get('aired', {}).get('from'):
            year = anime_data['aired']['from'][:4]
            
        # Determine season and adjusted episode number
        season_number, adjusted_episode = _determine_season_info(anime_data, episode_number)
            
        # Build metadata
        metadata = {
            'title': anime_data.get('title', item.get('title', 'Unknown')),
            'year': year or item.get('year', ''),
            'episode_title': episode_data.get('title', item.get('episode_title', '')) if episode_data else item.get('episode_title', ''),
            'episode_number': adjusted_episode,
            'season_number': season_number
        }
        
        logging.debug(f"Generated metadata: {metadata}")
        return metadata
        
    except Exception as e:
        logging.error(f"Error fetching anime metadata: {str(e)}")
        return None

def format_filename_with_anidb(item: Dict[str, Any], original_extension: str) -> Optional[str]:
    """Format filename using anime metadata from Jikan."""
    try:
        if not get_setting('Debug', 'use_anidb_metadata', False):
            logging.debug("Anime metadata is disabled")
            return None
            
        # Only process anime episodes
        if item.get('type') != 'episode' or not item.get('is_anime', False):
            logging.debug("Item is not an anime episode")
            return None
            
        # Get metadata
        metadata = get_anidb_metadata_for_item(item)
        if not metadata:
            return None
            
        # Get the template from settings
        template = get_setting('Debug', 'anidb_episode_template',
                             '{title} ({year})/Season {season_number:02d}/{title} ({year}) - S{season_number:02d}E{episode_number:02d} - {episode_title}')
        
        # Format using metadata
        template_vars = {
            'title': metadata.get('title', item.get('title', 'Unknown')),
            'year': metadata.get('year', item.get('year', '')),
            'season_number': int(metadata.get('season_number', item.get('season_number', 0))),
            'episode_number': int(metadata.get('episode_number', item.get('episode_number', 0))),
            'episode_title': metadata.get('episode_title', item.get('episode_title', '')),
        }
        
        filename = template.format(**template_vars)
        logging.debug(f"Formatted filename: {filename}")
        
        # Add extension if configured
        if get_setting('Debug', 'symlink_preserve_extension', True):
            if original_extension and not original_extension.startswith('.'):
                original_extension = f".{original_extension}"
            filename = f"{filename}{original_extension}"
            logging.debug(f"Final filename with extension: {filename}")
            
        return filename
        
    except Exception as e:
        logging.error(f"Error formatting filename with anime metadata: {str(e)}")
        return None 