import logging
import re
from typing import List, Dict, Any, Union
from database.database_reading import get_movie_runtime, get_episode_runtime, get_episode_count
from fuzzywuzzy import fuzz
from metadata.metadata import get_tmdb_id_and_media_type, get_metadata
from PTT import parse_title
from babelfish import Language
from scraper.functions import *
from scraper.functions.common import detect_season_episode_info
from functools import lru_cache

# Pre-compile regex patterns
_SEASON_RANGE_PATTERN = re.compile(r's(\d+)-s(\d+)')

@lru_cache(maxsize=1024)
def _parse_with_ptt(title: str) -> Dict[str, Any]:
    """Cached PTT parsing"""
    result = parse_title(title)
    logging.debug(f"PTT parsed '{title}' into: {result}")
    return result

def detect_hdr(parsed_info: Dict[str, Any]) -> bool:
    # Extract HDR information from PTT result
    hdr_info = parsed_info.get('hdr', False)
    if hdr_info:
        return True

    # Fallback to checking title for HDR terms
    title = parsed_info.get('title', '').upper()
    hdr_terms = ['HDR', 'DV', 'DOVI', 'DOLBY VISION', 'HDR10+', 'HDR10', 'HLG']
    for term in hdr_terms:
        if term in title:
            # Special case for 'DV' to exclude 'DVDRIP'
            if term == 'DV' and 'DVDRIP' in title:
                continue
            return True
    return False

def match_any_title(release_title: str, official_titles: List[str], threshold: float = 0.35) -> float:
    max_similarity = 0
    for title in official_titles:
        partial_score = partial_title_match(release_title, title)
        fuzzy_score = fuzzy_title_match(release_title, title) / 100
        similarity = max(partial_score, fuzzy_score)
        max_similarity = max(max_similarity, similarity)
        logging.debug(f"Matching '{release_title}' with '{title}': Partial: {partial_score:.2f}, Fuzzy: {fuzzy_score:.2f}, Max: {similarity:.2f}")
    return max_similarity

def partial_title_match(title1: str, title2: str) -> float:
    words1 = set(title1.lower().split())
    words2 = set(title2.lower().split())
    common_words = words1.intersection(words2)
    return len(common_words) / max(len(words1), len(words2))

def fuzzy_title_match(title1: str, title2: str) -> float:
    return fuzz.partial_ratio(title1.lower(), title2.lower())

def calculate_bitrate(size_gb, runtime_minutes):
    if not size_gb or not runtime_minutes:
        return 0
    size_bits = size_gb * 8 * 1024 * 1024 * 1024  # Convert GB to bits
    runtime_seconds = runtime_minutes * 60
    bitrate_mbps = (size_bits / runtime_seconds) / 1000  # Convert to Mbps
    return round(bitrate_mbps, 2)

def detect_resolution(parsed_info: Dict[str, Any]) -> str:
    # First try to get resolution from PTT result
    resolution = parsed_info.get('resolution', 'Unknown')
    return resolution

def batch_parse_torrent_info(titles: List[str], sizes: List[Union[str, int, float]] = None) -> List[Dict[str, Any]]:
    """
    Parse multiple torrent titles in batch for better performance using PTT.
    """
    if sizes is None:
        sizes = [None] * len(titles)
    
    results = []
    for title, size in zip(titles, sizes):
        try:
            # Check for unreasonable season ranges
            season_range_match = _SEASON_RANGE_PATTERN.search(title.lower())
            if season_range_match:
                start_season, end_season = map(int, season_range_match.groups())
                if end_season - start_season > 50:
                    results.append({'title': title, 'original_title': title, 'invalid_season_range': True})
                    continue
            
            # Parse with PTT (cached)
            try:
                parsed_info = _parse_with_ptt(title)
                
                # Convert PTT result to our standard format
                processed_info = {
                    'title': parsed_info.get('title', title),
                    'original_title': title,  # Store original title
                    'year': parsed_info.get('year'),
                    'resolution': parsed_info.get('resolution', 'Unknown'),
                    'source': parsed_info.get('source'),
                    'audio': parsed_info.get('audio'),
                    'codec': parsed_info.get('codec'),
                    'group': parsed_info.get('group'),
                    'season': parsed_info.get('season'),
                    'seasons': parsed_info.get('seasons'),  # Add seasons field
                    'episode': parsed_info.get('episode'),
                    'episodes': parsed_info.get('episodes'),
                    'type': parsed_info.get('type'),
                }
                
                # Handle size if provided
                if size is not None:
                    processed_info['size'] = parse_size(size)
                
                # Add additional information
                processed_info['resolution_rank'] = get_resolution_rank(processed_info['resolution'])
                processed_info['is_hdr'] = detect_hdr(parsed_info)
                
                # Extract season/episode info
                season_episode_info = detect_season_episode_info(processed_info)
                processed_info['season_episode_info'] = season_episode_info
                
                results.append(processed_info)
                
            except Exception as e:
                logging.error(f"PTT parsing error for '{title}': {str(e)}")
                results.append({'title': title, 'original_title': title, 'parsing_error': True})
                continue
            
        except Exception as e:
            logging.error(f"Error in parse_torrent_info for '{title}': {str(e)}", exc_info=True)
            results.append({'title': title, 'original_title': title, 'parsing_error': True})
    
    return results

def parse_torrent_info(title: str, size: Union[str, int, float] = None) -> Dict[str, Any]:
    """
    Parse a single torrent title. Uses the batch processing function for consistency.
    """
    results = batch_parse_torrent_info([title], [size])
    return results[0]

def get_media_info_for_bitrate(media_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    processed_items = []

    for item in media_items:
        try:
            if item['media_type'] == 'movie':
                db_runtime = get_movie_runtime(item['tmdb_id'])
                if db_runtime:
                    item['episode_count'] = 1
                    item['runtime'] = db_runtime
                else:
                    logging.info(f"Fetching metadata for movie: {item['title']}")
                    metadata = get_metadata(tmdb_id=item['tmdb_id'], item_media_type='movie')
                    item['runtime'] = metadata.get('runtime', 100)  # Default to 100 if not found
                    item['episode_count'] = 1
                    logging.info(f"Runtime for movie: {item['runtime']}")
        
            elif item['media_type'] == 'episode':
                db_runtime = get_episode_runtime(item['tmdb_id'])
                if db_runtime:
                    item['runtime'] = db_runtime
                    item['episode_count'] = get_episode_count(item['tmdb_id'])
                else:
                    logging.info(f"Fetching metadata for TV show: {item['title']}")
                    if 'imdb_id' in item:
                        tmdb_id, media_type = get_tmdb_id_and_media_type(item['imdb_id'])
                    elif 'tmdb_id' in item:
                        tmdb_id, media_type = item['tmdb_id'], 'tv'
                    else:
                        logging.warning(f"No IMDb ID or TMDB ID found for TV show: {item['title']}")
                        tmdb_id, media_type = None, None

                    if tmdb_id and media_type == 'tv':
                        metadata = get_metadata(tmdb_id=tmdb_id, item_media_type='tv')
                        item['runtime'] = metadata.get('runtime', 30)  # Default to 30 if not found
                        seasons = metadata.get('seasons', {})
                        item['episode_count'] = sum(season.get('episode_count', 0) for season in seasons.values())
                    else:
                        logging.warning(f"Could not fetch details for TV show: {item['title']}")
                        item['episode_count'] = 1
                        item['runtime'] = 30  # Default value
                    
                    logging.info(f"Runtime for TV show: {item['runtime']}")
                    logging.info(f"Episode count for TV show: {item['episode_count']}")
            
            logging.debug(f"Processed {item['title']}: {item['episode_count']} episodes, {item['runtime']} minutes per episode/movie")
            processed_items.append(item)

        except Exception as e:
            logging.error(f"Error processing item {item['title']}: {str(e)}")
            # Add item with default values in case of error
            item['episode_count'] = 1
            item['runtime'] = 30 if item['media_type'] == 'episode' else 100
            processed_items.append(item)

    return processed_items

def parse_size(size):
    if isinstance(size, (int, float)):
        return float(size)  # Assume it's already in GB
    elif isinstance(size, str):
        size = size.upper()
        if 'GB' in size:
            return float(size.replace('GB', '').strip())
        elif 'MB' in size:
            return float(size.replace('MB', '').strip()) / 1024
        elif 'KB' in size:
            return float(size.replace('KB', '').strip()) / (1024 * 1024)
    return 0  # Default to 0 if unable to parse

def get_resolution_rank(resolution: str) -> int:
    resolution = resolution.lower()
    
    if resolution in ['4k', '2160p']:
        return 4
    elif resolution in ['1080p', '1080i']:
        return 3
    elif resolution == '720p':
        return 2
    elif resolution in ['576p', '480p', 'sd']:
        return 1
    return 0  # For unknown resolutions

def compare_resolutions(res1: str, res2: str) -> int:
    resolution_order = {
        '2160p': 6, '4k': 6, 'uhd': 6,
        '1440p': 5, 'qhd': 5,
        '1080p': 4, 'fhd': 4,
        '720p': 3, 'hd': 3,
        '480p': 2, 'sd': 2,
        '360p': 1
    }

    val1 = resolution_order.get(res1.lower(), 0)
    val2 = resolution_order.get(res2.lower(), 0)

    return val1 - val2