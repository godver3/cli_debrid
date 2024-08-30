import PTN
import logging
import re
from api_tracker import api
from typing import List, Dict, Any, Tuple, Optional, Union
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor, as_completed
from .zilean import scrape_zilean
from .torrentio import scrape_torrentio
from .knightcrawler import scrape_knightcrawler
from .comet import scrape_comet
from .prowlarr import scrape_prowlarr
from .jackett import scrape_jackett
from settings import get_setting
import time
from metadata.metadata import get_overseerr_movie_details, get_overseerr_cookies, imdb_to_tmdb, get_overseerr_show_details, get_overseerr_show_episodes, get_episode_count_for_seasons, get_all_season_episode_counts
from pprint import pformat
import json
from fuzzywuzzy import fuzz
import os
from unidecode import unidecode
from utilities.plex_functions import filter_genres
from guessit import guessit
import pykakasi
from babelfish import Language

def romanize_japanese(text):
    kks = pykakasi.kakasi()
    result = kks.convert(text)
    return ' '.join([item['hepburn'] for item in result])

def setup_scraper_logger():
    log_dir = 'logs'
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    
    scraper_logger = logging.getLogger('scraper_logger')
    scraper_logger.setLevel(logging.DEBUG)
    scraper_logger.propagate = False  # Prevent propagation to the root logger
    
    # Remove all existing handlers
    for handler in scraper_logger.handlers[:]:
        scraper_logger.removeHandler(handler)
    
    file_handler = logging.FileHandler(os.path.join(log_dir, 'scraper.log'))
    file_handler.setLevel(logging.DEBUG)
    
    formatter = logging.Formatter('%(asctime)s - %(message)s')
    file_handler.setFormatter(formatter)
    
    scraper_logger.addHandler(file_handler)
    
    return scraper_logger

scraper_logger = setup_scraper_logger()

def log_filter_result(title: str, resolution: str, filter_reason: str = None):
    if filter_reason:
        logging.debug(f"Release: '{title}' (Resolution: {resolution}) - Filtered out: {filter_reason}")
    else:
        logging.debug(f"Release: '{title}' (Resolution: {resolution}) - Passed filters")

def detect_hdr(parsed_info: Dict[str, Any]) -> bool:
    other = parsed_info.get('other', [])
    
    # Convert 'other' to a list if it's not already (sometimes it might be a single string)
    if isinstance(other, str):
        other = [other]
    
    # List of HDR-related terms
    hdr_terms = ['HDR', 'DV', 'DOVI', 'DOLBY VISION', 'HDR10+', 'HDR10', 'HLG']
    
    # Check for HDR terms in the 'other' field
    for term in hdr_terms:
        if any(term.lower() in item.lower() for item in other):
            return True
    
    # If not found in 'other', check the title as a fallback
    title = parsed_info.get('title', '')
    title_upper = title.upper()
    for term in hdr_terms:
        if term in title_upper:
            # Special case for 'DV' to exclude 'DVDRIP'
            if term == 'DV' and 'DVDRIP' in title_upper:
                continue
            return True
    
    return False

def is_regex(pattern):
    """Check if a pattern is likely to be a regex."""
    return any(char in pattern for char in r'.*?+^$()[]{}|\\')

def smart_search(pattern, text):
    """Perform either regex search or simple string matching."""
    if is_regex(pattern):
        try:
            return re.search(pattern, text, re.IGNORECASE) is not None
        except re.error:
            # If regex is invalid, fall back to simple string matching
            return pattern.lower() in text.lower()
    else:
        return pattern.lower() in text.lower()

def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def improved_title_similarity(query_title: str, result: Dict[str, Any], is_anime: bool = False) -> float:
    
    # Normalize titles
    query_title = normalize_title(query_title)
    result_title = result.get('title', '')
    result_title = normalize_title(result_title)
    
    parsed_info = result.get('parsed_info', {})
    guessit_title = parsed_info.get('title', result_title)
    guessit_title = normalize_title(guessit_title)

    # For anime, consider alternative titles
    if is_anime:
        alternative_titles = parsed_info.get('alternative_title', [])
        if isinstance(alternative_titles, str):
            alternative_titles = [alternative_titles]
        alternative_titles = [normalize_title(alt) for alt in alternative_titles]

    # Normalize titles
    query_title = query_title.lower()
    guessit_title = guessit_title.lower()

    # Calculate token sort ratio
    token_sort_ratio = fuzz.token_sort_ratio(query_title, guessit_title)

    # Calculate token set ratio
    token_set_ratio = fuzz.token_set_ratio(query_title, guessit_title)

    # Check if the first word matches
    query_first_word = query_title.split()[0] if query_title else ''
    guessit_first_word = guessit_title.split()[0] if guessit_title else ''
    first_word_match = query_first_word == guessit_first_word

    # Check for additional words in guessit title
    query_words = set(query_title.split())
    guessit_words = set(guessit_title.split())
    additional_words = guessit_words - query_words

    # Calculate final similarity score
    similarity = (token_sort_ratio * 0.4) + (token_set_ratio * 0.4)  # 80% weight to fuzzy matching
    if first_word_match:
        similarity += 10  # 10% bonus for first word match

    # Penalty for additional words
    similarity -= len(additional_words) * 5  # 5% penalty per additional word

    # For anime, check alternative titles
    if is_anime:
        for alt_title in alternative_titles:
            alt_similarity = fuzz.token_set_ratio(query_title, alt_title.lower())
            similarity = max(similarity, alt_similarity)

    # Normalize the score to be between 0 and 100
    similarity = max(0, min(similarity, 100))

    return similarity / 100  # Return as a float between 0 and 1


def calculate_bitrate(size_gb, runtime_minutes):
    if not size_gb or not runtime_minutes:
        return 0
    size_bits = size_gb * 8 * 1024 * 1024 * 1024  # Convert GB to bits
    runtime_seconds = runtime_minutes * 60
    bitrate_mbps = (size_bits / runtime_seconds) / 1000  # Convert to Mbps
    return round(bitrate_mbps, 2)

def detect_resolution(parsed_info: Dict[str, Any]) -> str:
    screen_size = parsed_info.get('screen_size')
    
    if screen_size:
        # Convert guessit's screen_size to our standard format
        if screen_size in ['4K', '2160p']:
            return '2160p'
        elif screen_size in ['1080p', '1080i']:
            return '1080p'
        elif screen_size == '720p':
            return '720p'
        elif screen_size in ['576p', '480p']:
            return '480p'
    
    # If guessit couldn't detect the resolution, fall back to our existing method
    resolution_patterns = [
        (r'(?:^|\.)2160p(?:\.|$)', '2160p'),
        (r'(?:^|\.)(4k|uhd)(?:\.|$)', '2160p'),
        (r'(?:^|\.)1080p(?:\.|$)', '1080p'),
        (r'(?:^|\.)720p(?:\.|$)', '720p'),
        (r'(?:^|\.)480p(?:\.|$)', '480p'),
    ]
    title = parsed_info.get('title', '').lower()

    for pattern, res in resolution_patterns:
        if re.search(pattern, title, re.IGNORECASE):
            return res

    return 'Unknown'

def parse_torrent_info(title: str, size: Union[str, int, float] = None) -> Dict[str, Any]:
    try:
        parsed_info = guessit(title)
    except Exception as e:
        logging.error(f"Error parsing title with guessit: {str(e)}")
        parsed_info = {'title': title}  # Fallback to using the raw title

    # Convert Language objects to strings and ensure year is always a list
    for key, value in parsed_info.items():
        if isinstance(value, Language):
            parsed_info[key] = str(value)
        elif key == 'year' and not isinstance(value, list):
            parsed_info[key] = [value]
        elif isinstance(value, list):
            parsed_info[key] = [str(item) if isinstance(item, Language) else item for item in value]


    # Handle size
    if size is not None:
        parsed_info['size'] = parse_size(size)

    # Convert Language and Size-like objects to strings
    for key, value in parsed_info.items():
        if isinstance(value, Language):
            parsed_info[key] = str(value)
        elif hasattr(value, 'bytes'):  # Check for Size-like objects
            parsed_info[key] = str(value.bytes)
        elif isinstance(value, list):
            parsed_info[key] = [
                str(item) if isinstance(item, Language) else
                str(item.bytes) if hasattr(item, 'bytes') else
                item
                for item in value
            ]

    # Detect resolution and rank
    resolution = detect_resolution(parsed_info)
    parsed_info['resolution'] = resolution
    parsed_info['resolution_rank'] = get_resolution_rank(resolution)

    # Detect HDR
    parsed_info['is_hdr'] = detect_hdr(parsed_info)

    # Detect season and episode info
    season_episode_info = detect_season_episode_info(parsed_info)
    parsed_info['season_episode_info'] = season_episode_info

    return parsed_info

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

def get_media_info_for_bitrate(media_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Retrieve episode count and runtime information for given media items.
    
    Args:
    media_items (List[Dict[str, Any]]): List of media items to process.
    
    Returns:
    List[Dict[str, Any]]: List of media items with additional 'episode_count' and 'runtime' fields.
    """
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    if not overseerr_url or not overseerr_api_key:
        logging.error("Overseerr URL or API key not set. Please configure in settings.")
        return []

    cookies = get_overseerr_cookies(overseerr_url)
    processed_items = []

    for item in media_items:
        try:
            if item['media_type'] == 'movie':
                details = get_overseerr_movie_details(overseerr_url, overseerr_api_key, item['tmdb_id'], cookies)
                if details:
                    item['episode_count'] = 1
                    item['runtime'] = details.get('runtime', 100)  # Default to 100 minutes if not available
                else:
                    logging.warning(f"Could not fetch details for movie: {item['title']}")
                    item['episode_count'] = 1
                    item['runtime'] = 100  # Default value
            
            elif item['media_type'] == 'episode':
                show_details = get_overseerr_show_details(overseerr_url, overseerr_api_key, item['tmdb_id'], cookies)
                if show_details:
                    seasons = show_details.get('seasons', [])
                    item['episode_count'] = sum(season.get('episodeCount', 0) for season in seasons if season.get('seasonNumber', 0) != 0)
                    
                    # Try to get runtime from TMDB API first
                    tmdb_api_key = get_setting('TMDB', 'api_key')
                    if tmdb_api_key:
                        try:
                            first_season = next((s for s in seasons if s.get('seasonNumber', 0) != 0), None)
                            if first_season:
                                season_info = get_tmdb_season_info(item['tmdb_id'], first_season['seasonNumber'], tmdb_api_key)
                                if season_info and season_info.get('episodes'):
                                    item['runtime'] = season_info['episodes'][0].get('runtime', 30)
                                else:
                                    raise Exception("Failed to get episode runtime from TMDB")
                            else:
                                raise Exception("No valid season found")
                        except Exception as e:
                            logging.warning(f"Error fetching TMDB data: {str(e)}. Falling back to Overseerr data.")
                            # Fall back to Overseerr data
                            item['runtime'] = get_runtime_from_overseerr(seasons, overseerr_url, overseerr_api_key, item['tmdb_id'], cookies)
                    else:
                        # Use Overseerr data if TMDB API key is not available
                        item['runtime'] = get_runtime_from_overseerr(seasons, overseerr_url, overseerr_api_key, item['tmdb_id'], cookies)
                else:
                    logging.warning(f"Could not fetch details for TV show: {item['title']}")
                    item['episode_count'] = 1
                    item['runtime'] = 30  # Default value
            
            logging.debug(f"Processed {item['title']}: {item['episode_count']} episodes, {item['runtime']} minutes per episode/movie")
            processed_items.append(item)

        except Exception as e:
            logging.error(f"Error processing item {item['title']}: {str(e)}")
            # Add item with default values in case of error
            item['episode_count'] = 1
            item['runtime'] = 30 if item['media_type'] == 'episode' else 100
            processed_items.append(item)

    return processed_items

def get_runtime_from_overseerr(seasons, overseerr_url, overseerr_api_key, tmdb_id, cookies):
    if seasons:
        first_season = next((s for s in seasons if s.get('seasonNumber', 0) != 0), None)
        if first_season:
            season_details = get_overseerr_show_episodes(overseerr_url, overseerr_api_key, tmdb_id, first_season['seasonNumber'], cookies)
            first_episode = season_details.get('episodes', [{}])[0]
            return first_episode.get('runtime', 30)
    return 30  # Default runtime if no data is available

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

# Update preprocess_title function to not remove resolution information
def preprocess_title(title):
    # Remove only non-resolution quality terms
    terms_to_remove = ['web-dl', 'webrip', 'bluray', 'dvdrip']
    for term in terms_to_remove:
        title = re.sub(r'\b' + re.escape(term) + r'\b', '', title, flags=re.IGNORECASE)
    return title.strip()

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

def rank_result_key(result: Dict[str, Any], all_results: List[Dict[str, Any]], query: str, query_year: int, query_season: int, query_episode: int, multi: bool, content_type: str, version_settings: Dict[str, Any]) -> Tuple:
    torrent_title = result.get('title', '')
    parsed_info = result.get('parsed_info', {})
    extracted_title = parsed_info.get('title', torrent_title)
    torrent_year = parsed_info.get('year')
    torrent_season, torrent_episode = parsed_info.get('season'), parsed_info.get('episode')

    # Get user-defined weights
    resolution_weight = int(version_settings.get('resolution_weight', 3))
    hdr_weight = int(version_settings.get('hdr_weight', 3))
    similarity_weight = int(version_settings.get('similarity_weight', 3))
    size_weight = int(version_settings.get('size_weight', 3))
    bitrate_weight = int(version_settings.get('bitrate_weight', 3))

    # Calculate base scores
    title_similarity = similarity(extracted_title, query)
    resolution_score = parsed_info.get('resolution_rank', 0)
    hdr_score = 1 if parsed_info.get('is_hdr', False) and version_settings.get('enable_hdr', True) else 0

    scraper = result.get('scraper', '').lower()

    size = float(result['size'])  # Extract the numeric value from the size string
    bitrate = float(result['bitrate'])  # Extract the numeric value from the bitrate string
    
    # Calculate percentile ranks for size and bitrate
    all_sizes = [float(r['size']) for r in all_results]
    all_bitrates = [float(r['bitrate']) for r in all_results]

    def percentile_rank(value, all_values):
        return sum(1 for v in all_values if v <= value) / len(all_values) if all_values else 0

    size_percentile = percentile_rank(size, all_sizes)
    bitrate_percentile = percentile_rank(bitrate, all_bitrates)

    # Normalize scores to a 0-10 range
    normalized_similarity = title_similarity * 10
    normalized_resolution = min(resolution_score * 2.5, 10)  # Assuming max resolution score is 4
    normalized_hdr = hdr_score * 10
    normalized_size = size_percentile * 10
    normalized_bitrate = bitrate_percentile * 10

    # Apply weights
    weighted_similarity = normalized_similarity * similarity_weight
    weighted_resolution = normalized_resolution * resolution_weight
    weighted_hdr = normalized_hdr * hdr_weight
    weighted_size = normalized_size * size_weight
    weighted_bitrate = normalized_bitrate * bitrate_weight

    # Handle the case where torrent_year might be a list
    if isinstance(torrent_year, list):
        year_match = 5 if query_year in torrent_year else (1 if any(abs(query_year - y) <= 1 for y in torrent_year) else 0)
    else:
        year_match = 5 if query_year == torrent_year else (1 if abs(query_year - (torrent_year or 0)) <= 1 else 0)

    # Only apply season and episode matching for TV shows
    if content_type.lower() == 'episode':
        season_match = 5 if query_season == torrent_season else 0
        episode_match = 5 if query_episode == torrent_episode else 0
    else:
        season_match = 0
        episode_match = 0

    # Multi-pack handling (only for TV shows)
    multi_pack_score = 0
    single_episode_score = 0
    if content_type.lower() == 'episode':
        season_pack = result.get('season_pack', 'Unknown')
        is_multi_pack = season_pack != 'N/A' and season_pack != 'Unknown'
        
        if is_multi_pack:
            if season_pack == 'Complete':
                num_items = 100  # Assign a high value for complete series
            else:
                num_items = len(season_pack.split(','))
            
            is_queried_season_pack = str(query_season) in season_pack.split(',')
        else:
            num_items = 1
            is_queried_season_pack = False

        # Apply a bonus for multi-packs when requested, scaled by the number of items
        MULTI_PACK_BONUS = 20  # Base bonus
        multi_pack_score = (50 + (MULTI_PACK_BONUS * num_items)) if multi and is_queried_season_pack else 0

        # Penalize multi-packs when looking for single episodes
        SINGLE_EPISODE_PENALTY = -25
        single_episode_score = SINGLE_EPISODE_PENALTY if not multi and is_multi_pack and query_episode is not None else 0

    # Implement preferred filtering logic
    preferred_filter_score = 0
    torrent_title_lower = torrent_title.lower()

    # Apply preferred_filter_in bonus
    preferred_filter_in_breakdown = {}
    for pattern, weight in version_settings.get('preferred_filter_in', []):
        if smart_search(pattern, torrent_title):
            preferred_filter_score += weight
            preferred_filter_in_breakdown[pattern] = weight

    # Apply preferred_filter_out penalty
    preferred_filter_out_breakdown = {}
    for pattern, weight in version_settings.get('preferred_filter_out', []):
        if smart_search(pattern, torrent_title):
            preferred_filter_score -= weight
            preferred_filter_out_breakdown[pattern] = -weight

    # Combine scores
    total_score = (
        weighted_similarity +
        weighted_resolution +
        weighted_hdr +
        weighted_size +
        weighted_bitrate +
        (year_match * 5) +
        (season_match * 5) +
        (episode_match * 5) +
        multi_pack_score +
        single_episode_score +
        preferred_filter_score
    )

    # Content type matching score
    content_type_score = 0
    if content_type.lower() == 'movie':
        if re.search(r'(s\d{2}|e\d{2}|season|episode)', torrent_title, re.IGNORECASE):
            content_type_score = -500
    elif content_type.lower() == 'episode':
        # Check for clear TV show indicators
        tv_indicators = re.search(r'(s\d{2}|e\d{2}|season|episode)', torrent_title, re.IGNORECASE)
        year_range = re.search(r'\[(\d{4}).*?(\d{4})\]', torrent_title)
        
        if not tv_indicators:
            # If no clear TV indicators, check for year ranges typical of TV collections
            if year_range:
                start_year, end_year = map(int, year_range.groups())
                if end_year - start_year > 1:  # Spans multiple years, likely a TV show
                    content_type_score = 0
                else:
                    content_type_score = -500
            else:
                content_type_score = -500
    else:
        logging.warning(f"Unknown content type: {content_type} for result: {torrent_title}")

    # Add content_type_score to the total score
    total_score += content_type_score

    # Create a score breakdown
    score_breakdown = {
        'similarity_score': round(weighted_similarity, 2),
        'resolution_score': round(weighted_resolution, 2),
        'hdr_score': round(weighted_hdr, 2) if version_settings.get('enable_hdr', True) else 0,
        'size_score': round(weighted_size, 2),
        'bitrate_score': round(weighted_bitrate, 2),
        'year_match': year_match * 5,
        'season_match': season_match * 5,
        'episode_match': episode_match * 5,
        'multi_pack_score': multi_pack_score,
        'single_episode_score': single_episode_score,
        'preferred_filter_score': preferred_filter_score,
        'preferred_filter_in_breakdown': preferred_filter_in_breakdown,
        'preferred_filter_out_breakdown': preferred_filter_out_breakdown,
        'content_type_score': content_type_score,
        'total_score': round(total_score, 2)
    }
    # Add multi-pack information to the score breakdown
    score_breakdown['is_multi_pack'] = is_multi_pack if content_type.lower() == 'episode' else False
    score_breakdown['num_items'] = num_items if content_type.lower() == 'episode' else 1
    score_breakdown['multi_pack_score'] = multi_pack_score
    score_breakdown['single_episode_score'] = single_episode_score

    # Add version-specific information
    score_breakdown['version'] = {
        'max_resolution': version_settings.get('max_resolution', 'Not specified'),
        'enable_hdr': version_settings.get('enable_hdr', True),
        'weights': {
            'resolution': resolution_weight,
            'hdr': hdr_weight,
            'similarity': similarity_weight,
            'size': size_weight,
            'bitrate': bitrate_weight
        },
        'min_size_gb': version_settings.get('min_size_gb', 0.01)
    }

    # Add the score breakdown to the result
    result['score_breakdown'] = score_breakdown

    # Return negative total_score to sort in descending order
    return (-total_score, -year_match, -season_match, -episode_match)
    
def trim_magnet(magnet: str) -> str:
    return magnet.split('&dn=')[0] if '&dn=' in magnet else magnet

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

def round_size(size: str) -> int:
    try:
        # Convert size to float and round to nearest whole number
        return round(float(size))
    except ValueError:
        # If size can't be converted to float, return 0
        return 0

def deduplicate_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    unique_results = {}
    title_size_map = {}

    for index, result in enumerate(results):
        magnet = result.get('magnet', '')
        title = result.get('title', '').lower()  # Convert to lowercase for case-insensitive comparison
        size = result.get('size', '')
        rounded_size = round_size(size)

        # First check: Use magnet link
        if magnet:
            trimmed_magnet = trim_magnet(magnet)
            unique_id = trimmed_magnet
        else:
            unique_id = f"{title}_{rounded_size}"

        is_duplicate = False

        # Check for duplicates using magnet or title_size
        if unique_id in unique_results:
            is_duplicate = True
            existing_result = unique_results[unique_id]
        elif f"{title}_{rounded_size}" in title_size_map:
            is_duplicate = True
            existing_result = title_size_map[f"{title}_{rounded_size}"]

        if is_duplicate:
            #logging.debug(f"Existing: '{existing_result.get('title')}', New: '{title}'")
            if len(result) > len(existing_result):
                unique_results[unique_id] = result
                title_size_map[f"{title}_{rounded_size}"] = result
            elif len(result) == len(existing_result) and result.get('seeders', 0) > existing_result.get('seeders', 0):
                unique_results[unique_id] = result
                title_size_map[f"{title}_{rounded_size}"] = result
        else:
            unique_results[unique_id] = result
            title_size_map[f"{title}_{rounded_size}"] = result

    return list(unique_results.values())
    
def filter_results(results: List[Dict[str, Any]], tmdb_id: str, title: str, year: int, content_type: str, season: int, episode: int, multi: bool, version_settings: Dict[str, Any], runtime: int, episode_count: int, season_episode_counts: Dict[int, int], genres: List[str]) -> List[Dict[str, Any]]:
    filtered_results = []
    resolution_wanted = version_settings.get('resolution_wanted', '<=')
    max_resolution = version_settings.get('max_resolution', '2160p')
    min_size_gb = float(version_settings.get('min_size_gb', 0.01))
    filter_in = version_settings.get('filter_in', [])
    filter_out = version_settings.get('filter_out', [])
    enable_hdr = version_settings.get('enable_hdr', False)

    def resolution_filter(result_resolution, max_resolution, resolution_wanted):
        comparison = compare_resolutions(result_resolution, max_resolution)
        if resolution_wanted == '<=':
            return comparison <= 0
        elif resolution_wanted == '==':
            return comparison == 0
        elif resolution_wanted == '>=':
            return comparison >= 0
        return False

    # Fetch alternate title for anime
    alternate_title = None
    original_title = None
    is_anime = genres and 'anime' in [genre.lower() for genre in genres]
    if is_anime:
        logging.info(f"Anime detected for {title}. Fetching original title.")
        overseerr_url = get_setting('Overseerr', 'url')
        overseerr_api_key = get_setting('Overseerr', 'api_key')
        cookies = get_overseerr_cookies(overseerr_url)
        
        if content_type.lower() == 'movie':
            details = get_overseerr_movie_details(overseerr_url, overseerr_api_key, tmdb_id, cookies)
        else:
            details = get_overseerr_show_details(overseerr_url, overseerr_api_key, tmdb_id, cookies)
        
        if details:
            original_title = details.get('originalName')
            if original_title:
                # Split the title into Japanese and non-Japanese parts
                japanese_part = ''.join([char for char in original_title if '\u4e00' <= char <= '\u9fff' or '\u3040' <= char <= '\u309f' or '\u30a0' <= char <= '\u30ff'])
                non_japanese_part = ''.join([char for char in original_title if not ('\u4e00' <= char <= '\u9fff' or '\u3040' <= char <= '\u309f' or '\u30a0' <= char <= '\u30ff')])
                
                # Romanize only the Japanese part
                romanized_japanese = romanize_japanese(japanese_part)
                
                # Combine romanized Japanese with non-Japanese part
                alternate_title = (romanized_japanese + ' ' + non_japanese_part).strip()
                
                logging.info(f"Original title: {original_title}")
                logging.info(f"Romanized title: {alternate_title}")
            else:
                logging.info("No original title found in Overseerr details.")
        else:
            logging.info("No details found in Overseerr.")

    for result in results:
        parsed_info = result.get('parsed_info', {})
        season_episode_info = parsed_info.get('season_episode_info', {})
        season_pack = season_episode_info.get('season_pack', 'Unknown')

        original_title = result.get('title', '')
        parsed_info = result.get('parsed_info', {})
        detected_resolution = parsed_info.get('resolution', 'Unknown')
        
        # Check title similarity
        title_sim = improved_title_similarity(title, result, is_anime)
        alternate_title_sim = improved_title_similarity(alternate_title, result, is_anime) if alternate_title else 0
        
        if "UFC" in result['title'].upper():
            similarity_threshold = 0.35
        else:
            similarity_threshold = 0.6
        
        if is_anime:
            if max(title_sim, alternate_title_sim) < similarity_threshold:
                result['filter_reason'] = f"Low title similarity: {max(title_sim, alternate_title_sim):.2f}"
                continue
        elif title_sim < similarity_threshold:
            result['filter_reason'] = f"Low title similarity: {title_sim:.2f}"
            continue

        # Apply resolution filter
        if not resolution_filter(detected_resolution, max_resolution, resolution_wanted):
            result['filter_reason'] = f"Resolution mismatch (max: {max_resolution}, wanted: {resolution_wanted})"
            continue

        # Apply HDR filter
        is_hdr = parsed_info.get('is_hdr', False)
        if not enable_hdr and is_hdr:
            result['filter_reason'] = "HDR content when HDR is disabled"
            continue

        # Content type specific filtering
        if content_type.lower() == 'movie':
            parsed_year = parsed_info.get('year')
            # Check if the title contains "UFC"
            if "UFC" not in original_title.upper():
                # Handle year filtering
                if parsed_year:
                    if isinstance(parsed_year, list):
                        # If any year in the list is within 1 year of the target year, keep the result
                        if not any(abs(int(py) - year) <= 1 for py in parsed_year):
                            result['filter_reason'] = f"Year mismatch: {parsed_year} (expected: {year})"
                            continue
                    else:
                        # If it's a single year, use the original logic
                        if abs(int(parsed_year) - year) > 1:
                            result['filter_reason'] = f"Year mismatch: {parsed_year} (expected: {year})"
                            continue
            else:
                # For UFC titles, we don't filter based on year
                logging.debug(f"Skipping year filter for UFC title: {original_title}")
        elif content_type.lower() == 'episode':
            if multi:
                if re.search(r'S\d{2}E\d{2}', original_title, re.IGNORECASE):
                    result['filter_reason'] = "Single episode result when searching for multi"
                    continue
            else:
                result_season = parsed_info.get('season')
                result_episode = parsed_info.get('episode')
                
                # If no season is specified, assume it's season 1
                if result_season is None and result_episode is not None:
                    result_season = 1
                
                # Log the detected and requested season/episode
                logging.debug(f"Detected: S{result_season}E{result_episode}, Requested: S{season}E{episode}")
                
                # Check if the episode matches
                if result_episode != episode:
                    result['filter_reason'] = f"Episode mismatch: E{result_episode} vs E{episode}"
                    continue
                
                # Check if the season matches, allowing season 1 if not specified
                if result_season != season and result_season != 1:
                    result['filter_reason'] = f"Season mismatch: S{result_season} vs S{season}"
                    continue

        size_gb = parse_size(result.get('size', 0))

        season_episode_info = result.get('parsed_info', {}).get('season_episode_info', {})
        scraper = result.get('scraper', '').lower()

        if scraper in ['jackett', 'zilean']:
            if season_episode_info.get('season_pack', 'Unknown') == 'N/A':
                if season_episode_info['season_pack'] == 'Complete':
                    total_episodes = sum(season_episode_counts.values())
                else:
                    season_numbers = [int(s) for s in season_episode_info['season_pack'].split(',')]
                    total_episodes = sum(season_episode_counts.get(s, 0) for s in season_numbers)
                
                if total_episodes > 0:
                    size_per_episode_gb = size_gb / total_episodes
                else:
                    size_per_episode_gb = size_gb
            else:
                size_per_episode_gb = size_gb

            result['size'] = size_per_episode_gb
            bitrate = calculate_bitrate(size_per_episode_gb, runtime)

        else:
            result['size'] = size_gb
            bitrate = calculate_bitrate(size_gb, runtime)

        result['bitrate'] = bitrate          

        # Apply custom filters with smart matching
        if filter_in and not any(smart_search(pattern, original_title) for pattern in filter_in):
            result['filter_reason'] = "Not matching any filter_in patterns"
            continue
        if filter_out:
            matched_patterns = [pattern for pattern in filter_out if smart_search(pattern, original_title)]
            if matched_patterns:
                result['filter_reason'] = f"Matching filter_out pattern(s): {', '.join(matched_patterns)}"
                continue

            if content_type.lower() == 'episode':
                if multi:
                    if season_episode_info['season_pack'] == 'N/A':
                        result['filter_reason'] = "Single episode result when searching for multi"
                        continue
                    if not season_episode_info['seasons'] and not season_episode_info['multi_episode']:
                        result['filter_reason'] = "Non-multi result when searching for multi"
                        continue
                    if season_episode_info['season_pack'] != 'Complete':
                        if season not in season_episode_info['seasons']:
                            result['filter_reason'] = f"Season pack not containing the requested season: {season}"
                            continue
                else:
                    if season_episode_info['season_pack'] != 'Unknown' and season_episode_info['season_pack'] != 'N/A' and season_episode_info['season_pack'] != str(season):
                        result['filter_reason'] = "Multi-season release when searching for single season"
                        continue
                    if season_episode_info['multi_episode'] and episode not in season_episode_info['episodes']:
                        result['filter_reason'] = "Multi-episode release not containing the requested episode"
                        continue
                    
                    # Check if the detected season and episode match the requested ones
                    detected_season = season_episode_info['seasons'][0] if season_episode_info['seasons'] else 1  # Assume season 1 if not detected
                    detected_episode = season_episode_info['episodes'][0] if season_episode_info['episodes'] else None
                    
                    logging.info(f"Comparing - Detected: S{detected_season}E{detected_episode}, Requested: S{season}E{episode}")
                    
                    if detected_episode != episode:
                        result['filter_reason'] = f"Episode mismatch: E{detected_episode} vs E{episode}"
                        continue
                    if detected_season != season and detected_season != 1:  # Allow season 1 if it doesn't match the requested season
                        result['filter_reason'] = f"Season mismatch: S{detected_season} vs S{season}"
                        continue

        # If the result passed all filters, add it to filtered_results
        filtered_results.append(result)

    return filtered_results

def normalize_title(title: str) -> str:
    """
    Normalize the title by replacing spaces with periods, removing colons,
    replacing ".~." with "-", and removing duplicate periods.
    """
    # Replace spaces with periods and remove colons
    normalized = title.replace(' ', '.').replace(':', '')
    
    # Replace ".~." with "-"
    normalized = normalized.replace('.~.', '-')
    
    # Remove duplicate periods
    normalized = re.sub(r'\.+', '.', normalized)
    
    # Remove leading and trailing periods
    normalized = normalized.strip('.')
    
    return normalized.lower()  # Convert to lowercase for case-insensitive comparison

def scrape(imdb_id: str, tmdb_id: str, title: str, year: int, content_type: str, version: str, season: int = None, episode: int = None, multi: bool = False, genres: List[str] = None) -> Tuple[List[Dict[str, Any]], Optional[List[Dict[str, Any]]]]:

    genres = filter_genres(genres)

    try:
        start_time = time.time()
        all_results = []

        logging.debug(f"Starting scraping for: {title} ({year}), Version: {version}")

        # Ensure content_type is correctly set
        if content_type.lower() not in ['movie', 'episode']:
            logging.warning(f"Invalid content_type: {content_type}. Defaulting to 'movie'.")
            content_type = 'movie'

        # Get TMDB ID and runtime once for all results
        overseerr_url = get_setting('Overseerr', 'url')
        overseerr_api_key = get_setting('Overseerr', 'api_key')
        cookies = get_overseerr_cookies(overseerr_url)

        # Get media info for bitrate calculation
        media_item = {
            'title': title,
            'media_type': 'movie' if content_type.lower() == 'movie' else 'episode',
            'tmdb_id': tmdb_id
        }
        enhanced_media_items = get_media_info_for_bitrate([media_item])
        if enhanced_media_items:
            episode_count = enhanced_media_items[0]['episode_count']
            runtime = enhanced_media_items[0]['runtime']
        else:
            episode_count = 1
            runtime = 100 if content_type.lower() == 'movie' else 30

        # Pre-calculate episode counts for TV shows
        season_episode_counts = {}
        if content_type.lower() == 'episode':
            season_episode_counts = get_all_season_episode_counts(overseerr_url, overseerr_api_key, tmdb_id, cookies)

        logging.debug(f"Retrieved runtime for {title}: {runtime} minutes, Episode count: {episode_count}")

        # Parse scraping settings based on version
        scraping_versions = get_setting('Scraping', 'versions', {})
        version_settings = scraping_versions.get(version, None)
        if version_settings is None:
            logging.warning(f"Version {version} not found in settings. Using default settings.")
            version_settings = {}
        logging.debug(f"Using version settings: {version_settings}")

        # Get all scraper settings
        all_scraper_settings = get_setting('Scrapers')

        # Define scraper functions
        scraper_functions = {
            'Zilean': scrape_zilean,
            'Torrentio': scrape_torrentio,
            'Comet': scrape_comet,
            'Jackett': scrape_jackett,
            'Prowlarr': scrape_prowlarr
        }

        def run_scraper(scraper_name, scraper_settings):
            scraper_start = time.time()
            try:
                logging.debug(f"Starting {scraper_name} scraper")
                #logging.debug(f"Scraper settings: {scraper_settings}")
                
                scraper_type = scraper_name.split('_')[0]
                scraper_func = scraper_functions.get(scraper_type)
                if not scraper_func:
                    logging.warning(f"No scraper function found for {scraper_name}")
                    return []
                
                # All scrapers now use the same parameter list
                scraper_results = scraper_func(imdb_id, title, year, content_type, season, episode, multi)
                                
                if isinstance(scraper_results, tuple):
                    *_, scraper_results = scraper_results
                for item in scraper_results:
                    item['scraper'] = scraper_name
                    item['title'] = normalize_title(item.get('title', ''))
                    item['parsed_info'] = parse_torrent_info(item['title'])  # Parse info once
                logging.debug(f"{scraper_name} scraper found {len(scraper_results)} results")
                logging.debug(f"{scraper_name} scraper took {time.time() - scraper_start:.2f} seconds")
                return scraper_results
            except Exception as e:
                logging.error(f"Error in {scraper_name} scraper: {str(e)}", exc_info=True)
                return []

        scraping_start = time.time()
        with ThreadPoolExecutor(max_workers=len(all_scraper_settings)) as executor:
            future_to_scraper = {}
            for scraper_name, scraper_settings in all_scraper_settings.items():
                if scraper_settings.get('enabled', False):
                    future = executor.submit(run_scraper, scraper_name, scraper_settings)
                    future_to_scraper[future] = scraper_name
                else:
                    logging.debug(f"Scraper {scraper_name} is disabled, skipping")

            for future in as_completed(future_to_scraper):
                scraper_name = future_to_scraper[future]
                try:
                    results = future.result()
                    all_results.extend(results)
                except Exception as e:
                    logging.error(f"Scraper {scraper_name} generated an exception: {str(e)}")

        logging.debug(f"Total scraping time: {time.time() - scraping_start:.2f} seconds")
        logging.debug(f"Total results before filtering: {len(all_results)}")

        # Deduplicate results before filtering
        all_results = deduplicate_results(all_results)
        logging.debug(f"Total results after deduplication: {len(all_results)}")

        # Filter results
        filtered_results = filter_results(all_results, tmdb_id, title, year, content_type, season, episode, multi, version_settings, runtime, episode_count, season_episode_counts, genres)
        filtered_out_results = [result for result in all_results if result not in filtered_results]

        logging.debug(f"Filtering took {time.time() - scraping_start:.2f} seconds")
        logging.debug(f"Total results after filtering: {len(filtered_results)}")
        logging.debug(f"Total filtered out results: {len(filtered_out_results)}")

        # Add is_multi_pack information to each result
        for result in filtered_results:
            torrent_title = result.get('title', '')
            size = result.get('size', 0)
            result['parsed_info'] = parse_torrent_info(torrent_title, size)
            preprocessed_title = preprocess_title(torrent_title)
            preprocessed_title = normalize_title(preprocessed_title)
            season_episode_info = detect_season_episode_info(preprocessed_title)
            season_pack = season_episode_info['season_pack']
            is_multi_pack = season_pack != 'N/A' and season_pack != 'Unknown'
            result['is_multi_pack'] = is_multi_pack
            result['season_pack'] = season_pack

        # Sort results
        sorting_start = time.time()

        def stable_rank_key(x):
            parsed_info = x.get('parsed_info', {})
            primary_key = rank_result_key(x, filtered_results, title, year, season, episode, multi, content_type, version_settings)
            secondary_keys = (
                x.get('scraper', ''),
                x.get('title', ''),
                x.get('size', 0),
                x.get('seeders', 0)
            )
            return (primary_key, secondary_keys)

        final_results = sorted(filtered_results, key=stable_rank_key)

        logging.debug(f"Sorting took {time.time() - sorting_start:.2f} seconds")

        logging.debug(f"Total results in final output: {len(final_results)}")
        logging.debug(f"Total scraping process took {time.time() - start_time:.2f} seconds")

        # Log to scraper.log
        scraper_logger.info(f"Scraping results for: {title} ({year})")
        scraper_logger.info("All result titles:")
        for result in all_results:
            scraper_logger.info(f"- {result.get('title', '')}")

        scraper_logger.info("Filtered out results:")
        for result in filtered_out_results:
            filter_reason = result.get('filter_reason', 'Unknown reason')
            scraper_logger.info(f"- {result.get('title', '')}: {filter_reason}")

        scraper_logger.info("Final results:")
        for result in final_results:
            result_info = (
                f"- {result.get('title', '')}: "
                f"Size: {result.get('size', 'N/A')} GB, "
                f"Bitrate: {result.get('bitrate', 'N/A')} Mbps, "
                f"Multi-pack: {'Yes' if result.get('is_multi_pack', False) else 'No'}, "
                f"Season pack: {result.get('season_pack', 'N/A')}"
            )
            scraper_logger.info(result_info)

        return final_results, filtered_out_results if filtered_out_results else None

    except Exception as e:
        logging.error(f"Unexpected error in scrape function for {title} ({year}): {str(e)}", exc_info=True)
        return [], []  # Return empty lists in case of an error
