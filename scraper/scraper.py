import PTN
import logging
import re
import requests
from typing import List, Dict, Any, Tuple, Optional
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

def log_filter_result(title: str, resolution: str, filter_reason: str = None):
    if filter_reason:
        logging.debug(f"Release: '{title}' (Resolution: {resolution}) - Filtered out: {filter_reason}")
    else:
        logging.debug(f"Release: '{title}' (Resolution: {resolution}) - Passed filters")

def detect_hdr(title: str) -> bool:
    # Convert title to uppercase for case-insensitive matching
    upper_title = title.upper()
    
    # List of HDR-related terms
    hdr_terms = ['HDR', 'DV', 'DOVI', 'DOLBY VISION', 'DOLBY.VISION', 'HDR10+', 'HDR10PLUS', 'HDR10']
    
    # Check for HDR terms, ensuring they are not part of other words
    for term in hdr_terms:
        # Use word boundaries to ensure we're matching whole words or abbreviations
        if re.search(r'\b' + re.escape(term) + r'\b', upper_title):
            # Special case for 'DV' to exclude 'DVDRIP'
            if term == 'DV' and 'DVDRIP' in upper_title:
                continue
            return True
    
    # Check for HLG (Hybrid Log-Gamma) separately as it doesn't need word boundaries
    if 'HLG' in upper_title:
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

def improved_title_similarity(query_title: str, result_title: str) -> float:
    # Parse the result title using PTN
    parsed_result = PTN.parse(result_title)
    ptn_title = parsed_result.get('title', result_title)

    # Normalize titles
    query_title = query_title.lower()
    ptn_title = ptn_title.lower()

    # Calculate token sort ratio
    token_sort_ratio = fuzz.token_sort_ratio(query_title, ptn_title)

    # Calculate token set ratio
    token_set_ratio = fuzz.token_set_ratio(query_title, ptn_title)

    # Check if the first word matches
    query_first_word = query_title.split()[0] if query_title else ''
    ptn_first_word = ptn_title.split()[0] if ptn_title else ''
    first_word_match = query_first_word == ptn_first_word

    # Check for additional words in PTN title
    query_words = set(query_title.split())
    ptn_words = set(ptn_title.split())
    additional_words = ptn_words - query_words

    # Calculate final similarity score
    similarity = (token_sort_ratio * 0.4) + (token_set_ratio * 0.4)  # 80% weight to fuzzy matching
    if first_word_match:
        similarity += 10  # 10% bonus for first word match

    # Penalty for additional words
    similarity -= len(additional_words) * 5  # 5% penalty per additional word

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

def detect_resolution(title: str) -> str:
    logging.debug(f"Detecting resolution for title: '{title}'")
    resolution_patterns = [
        (r'(?:^|\.)2160p(?:\.|$)', '2160p'),
        (r'(?:^|\.)(4k|uhd)(?:\.|$)', '2160p'),
        (r'(?:^|\.)1080p(?:\.|$)', '1080p'),
        (r'(?:^|\.)720p(?:\.|$)', '720p'),
        (r'(?:^|\.)480p(?:\.|$)', '480p'),
    ]
    title_lower = title.lower()

    detected_resolutions = []

    # Check for all resolution patterns
    for pattern, res in resolution_patterns:
        if re.search(pattern, title_lower, re.IGNORECASE):
            detected_resolutions.append(res)
            logging.debug(f"Matched pattern '{pattern}' in title. Detected resolution: {res}")

    # Look for numbers followed by 'p' surrounded by periods
    p_matches = re.findall(r'(?:^|\.)(\d+)p(?:\.|$)', title_lower)
    for p_value in p_matches:
        p_value = int(p_value)
        if p_value >= 2160:
            detected_resolutions.append('2160p')
        elif p_value >= 1080:
            detected_resolutions.append('1080p')
        elif p_value >= 720:
            detected_resolutions.append('720p')
        elif p_value >= 480:
            detected_resolutions.append('480p')

    if detected_resolutions:
        # Sort resolutions and pick the highest
        resolution_order = ['480p', '720p', '1080p', '2160p']
        highest_resolution = max(detected_resolutions, key=lambda x: resolution_order.index(x))
        logging.debug(f"Multiple resolutions detected: {detected_resolutions}. Choosing highest: {highest_resolution}")
        return highest_resolution
    else:
        logging.debug("No resolution detected. Returning 'Unknown'")
        return 'Unknown'

# Update parse_torrent_info to use our detect_resolution function
def parse_torrent_info(title: str) -> Dict[str, Any]:
    parsed_info = PTN.parse(title)
    detected_resolution = detect_resolution(title)
    parsed_info['resolution'] = detected_resolution
    return parsed_info

def get_tmdb_season_info(tmdb_id: int, season_number: int, api_key: str) -> Optional[Dict[str, Any]]:
    url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_number}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "accept": "application/json"
    }
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
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
                        first_season = next((s for s in seasons if s.get('seasonNumber', 0) != 0), None)
                        if first_season:
                            season_info = get_tmdb_season_info(item['tmdb_id'], first_season['seasonNumber'], tmdb_api_key)
                            if season_info and season_info.get('episodes'):
                                item['runtime'] = season_info['episodes'][0].get('runtime', 30)
                            else:
                                item['runtime'] = 30
                        else:
                            item['runtime'] = 30
                    else:
                        # Fallback to Overseerr data if TMDB API key is not available
                        if seasons:
                            first_season = next((s for s in seasons if s.get('seasonNumber', 0) != 0), None)
                            if first_season:
                                season_details = get_overseerr_show_episodes(overseerr_url, overseerr_api_key, item['tmdb_id'], first_season['seasonNumber'], cookies)
                                first_episode = season_details.get('episodes', [{}])[0]
                                item['runtime'] = first_episode.get('runtime', 30)
                            else:
                                item['runtime'] = 30
                        else:
                            item['runtime'] = 30
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

def detect_season_pack(title: str) -> str:
    normalized_title = normalize_title(title)
    parsed = PTN.parse(normalized_title)
    
    if 'season' in parsed:
        seasons = parsed['season']
        if isinstance(seasons, list):
            return ','.join(str(s) for s in sorted(set(seasons)))
        else:
            return str(seasons)
    
    if re.search(r'\b(complete series|all seasons|all episodes)\b', normalized_title, re.IGNORECASE):
        return 'Complete'
    
    range_match = re.search(r'(?:s|season)\.?(\d{1,2})\.?-\.?(?:s|season)?\.?(\d{1,2})', normalized_title, re.IGNORECASE)
    if range_match:
        start, end = map(int, range_match.groups())
        return ','.join(str(s) for s in range(start, end + 1))
    
    consecutive_seasons = re.findall(r'(?<!\d)(\d{1,2})(?=[.\s]|$)', normalized_title)
    if len(consecutive_seasons) > 1:
        seasons = sorted(set(map(int, consecutive_seasons)))
        return ','.join(map(str, seasons))
    
    return 'Unknown'

def get_resolution_rank(quality: str) -> int:
    quality = quality.lower()
    parsed = PTN.parse(quality)
    resolution = parsed.get('resolution', '').lower()
    
    if resolution:
        if compare_resolutions(resolution, '2160p') >= 0:
            return 4
        elif compare_resolutions(resolution, '1080p') >= 0:
            return 3
        elif compare_resolutions(resolution, '720p') >= 0:
            return 2
        elif compare_resolutions(resolution, 'sd') >= 0:
            return 1
    return 0  # For unknown resolutions

def extract_season_episode(text: str) -> Tuple[int, int]:
    season_episode_pattern = r'S(\d+)(?:E(\d+))?'
    match = re.search(season_episode_pattern, text, re.IGNORECASE)
    if match:
        season = int(match.group(1))
        episode = int(match.group(2)) if match.group(2) else None
        return season, episode
    return None, None

def extract_title_and_se(torrent_name: str) -> Tuple[str, int, int]:
    parsed = PTN.parse(torrent_name)
    title = parsed.get('title', torrent_name)
    season = parsed.get('season')
    episode = parsed.get('episode')
    return title, season, episode

def rank_result_key(result: Dict[str, Any], all_results: List[Dict[str, Any]], query: str, query_year: int, query_season: int, query_episode: int, multi: bool, content_type: str, version_settings: Dict[str, Any]) -> Tuple:
    torrent_title = result.get('title', '')
    parsed = result.get('parsed_info', {})
    extracted_title = parsed.get('title', torrent_title)
    torrent_year = parsed.get('year')
    torrent_season, torrent_episode = parsed.get('season'), parsed.get('episode')

    # Get user-defined weights
    resolution_weight = int(version_settings.get('resolution_weight', 3))
    hdr_weight = int(version_settings.get('hdr_weight', 3))
    similarity_weight = int(version_settings.get('similarity_weight', 3))
    size_weight = int(version_settings.get('size_weight', 3))
    bitrate_weight = int(version_settings.get('bitrate_weight', 3))

    # Calculate base scores
    title_similarity = similarity(extracted_title, query)
    resolution_score = get_resolution_rank(torrent_title)
    hdr_score = 1 if result.get('is_hdr', False) and version_settings.get('enable_hdr', True) else 0

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

    # Existing logic for year, season, and episode matching
    year_match = 5 if query_year == torrent_year else (1 if abs(query_year - (torrent_year or 0)) <= 1 else 0)
    season_match = 5 if query_season == torrent_season else 0
    episode_match = 5 if query_episode == torrent_episode else 0

    # Multi-pack handling
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
    score_breakdown['is_multi_pack'] = is_multi_pack
    score_breakdown['num_items'] = num_items
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

# Make sure to keep the compare_resolutions function as it is used in the resolution_filter
def compare_resolutions(res1: str, res2: str) -> int:
    resolution_order = {
        '2160p': 6, '4k': 6, 'uhd': 6,
        '1440p': 5, 'qhd': 5,
        '1080p': 4, 'fhd': 4,
        '720p': 3, 'hd': 3,
        '480p': 2, 'sd': 2,
        '360p': 1
    }

    res1 = res1.lower()
    res2 = res2.lower()

    val1 = resolution_order.get(res1, 0)
    val2 = resolution_order.get(res2, 0)

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
    
def filter_results(results: List[Dict[str, Any]], tmdb_id: str, title: str, year: int, content_type: str, season: int, episode: int, multi: bool, version_settings: Dict[str, Any], runtime: int, episode_count: int, season_episode_counts: Dict[int, int]) -> List[Dict[str, Any]]:
    filtered_results = []
    resolution_wanted = version_settings.get('resolution_wanted', '<=')
    max_resolution = version_settings.get('max_resolution', '2160p')
    min_size_gb = float(version_settings.get('min_size_gb', 0.01))
    filter_in = version_settings.get('filter_in', [])
    filter_out = version_settings.get('filter_out', [])
    enable_hdr = version_settings.get('enable_hdr', False)

    def resolution_filter(result_resolution):
        comparison = compare_resolutions(result_resolution, max_resolution)
        if resolution_wanted == '<=':
            return comparison <= 0
        elif resolution_wanted == '==':
            return comparison == 0
        elif resolution_wanted == '>=':
            return comparison >= 0
        return False

    for result in results:
        original_title = result.get('title', '')
        detected_resolution = detect_resolution(original_title)
        preprocessed_title = preprocess_title(original_title)
        parsed_info = parse_torrent_info(preprocessed_title)
        parsed_info['resolution'] = detected_resolution

        # Apply resolution filter
        if not resolution_filter(detected_resolution):
            log_filter_result(original_title, detected_resolution, f"Resolution mismatch (max: {max_resolution}, wanted: {resolution_wanted})")
            continue

        # Apply HDR filter
        is_hdr = detect_hdr(original_title)
        if not enable_hdr and is_hdr:
            log_filter_result(original_title, detected_resolution, "HDR content when HDR is disabled")
            continue

        # Check title similarity
        title_sim = improved_title_similarity(title, original_title)
        #logging.debug(f"Title similarity for '{original_title}': {title_sim:.2f}")
        if title_sim < 0.85:  # Increased threshold from 0.8 to 0.7
            logging.debug(f"Filtered out due to low title similarity: {title_sim:.2f}")
            continue       

        # Content type specific filtering
        if content_type.lower() == 'movie':
            parsed_year = parsed_info.get('year')
            if not parsed_year:
                log_filter_result(original_title, detected_resolution, "Missing year")
                continue
            if abs(int(parsed_year) - year) > 1:
                log_filter_result(original_title, detected_resolution, f"Year mismatch: {parsed_year} vs {year}")
                continue
        elif content_type.lower() == 'episode':
            if multi:
                if re.search(r'S\d{2}E\d{2}', original_title, re.IGNORECASE):
                    log_filter_result(original_title, detected_resolution, "Single episode result when searching for multi")
                    continue
            else:
                result_season = parsed_info.get('season')
                result_episode = parsed_info.get('episode')
                if result_season != season or result_episode != episode:
                    log_filter_result(original_title, detected_resolution, f"Season/episode mismatch: S{result_season}E{result_episode} vs S{season}E{episode}")
                    continue

        size_gb = parse_size(result.get('size', 0))
        season_pack = detect_season_pack(original_title)
        scraper = result.get('scraper', '').lower()

        if scraper in ['jackett', 'zilean']:
            if season_pack != 'Unknown' and season_pack != 'N/A':
                if season_pack == 'Complete':
                    total_episodes = sum(season_episode_counts.values())
                else:
                    season_numbers = [int(s) for s in season_pack.split(',')]
                    total_episodes = sum(season_episode_counts.get(s, 0) for s in season_numbers)
                
                if total_episodes > 0:
                    size_per_episode_gb = size_gb / total_episodes
                else:
                    size_per_episode_gb = size_gb
            else:
                size_per_episode_gb = size_gb

            result['size'] = size_per_episode_gb
            #result['size'] = f"{size_per_episode_gb:.2f} GB (per episode)"
            bitrate = calculate_bitrate(size_per_episode_gb, runtime)

        else:
            result['size'] = size_gb
            #result['size'] = f"{size_gb:.2f} GB"
            bitrate = calculate_bitrate(size_gb, runtime) #* episode_count)

        result['bitrate'] = bitrate

        # Apply custom filters with smart matching
        if filter_in and not any(smart_search(pattern, original_title) for pattern in filter_in):
            log_filter_result(original_title, detected_resolution, "Not matching any filter_in patterns")
            continue
        if filter_out:
            matched_patterns = [pattern for pattern in filter_out if smart_search(pattern, original_title)]
            if matched_patterns:
                log_filter_result(original_title, detected_resolution, f"Matching filter_out pattern(s): {', '.join(matched_patterns)}")
                continue

        season_pack = detect_season_pack(original_title)
        if multi:
            if season_pack == 'Unknown':
                log_filter_result(original_title, detected_resolution, "Non-multi result when searching for multi")
                continue
            if season_pack != 'Complete':
                season_numbers = [int(s) for s in season_pack.split(',')]
                if len(season_numbers) == 2:
                    # It's a range
                    if season not in range(season_numbers[0], season_numbers[1] + 1):
                        log_filter_result(original_title, detected_resolution, f"Season pack not containing the requested season: {season}")
                        continue
                elif season not in season_numbers:
                    log_filter_result(original_title, detected_resolution, f"Season pack not containing the requested season: {season}")
                    continue
        else:
            if season_pack != 'Unknown' and season_pack != str(season):
                log_filter_result(original_title, detected_resolution, "Multi-episode release when searching for single episode")
                continue

        # If the result passed all filters, add it to filtered_results
        log_filter_result(original_title, detected_resolution)
        filtered_results.append(result)

    return filtered_results

def normalize_title(title: str) -> str:
    """
    Normalize the title by replacing spaces with periods, removing colons,
    and removing duplicate periods.
    """
    # Replace spaces with periods and remove colons
    normalized = title.replace(' ', '.').replace(':', '')
    # Remove duplicate periods
    normalized = re.sub(r'\.+', '.', normalized)
    # Remove leading and trailing periods
    normalized = normalized.strip('.')
    return normalized.lower()  # Convert to lowercase for case-insensitive comparison

def scrape(imdb_id: str, tmdb_id: str, title: str, year: int, content_type: str, version: str, season: int = None, episode: int = None, multi: bool = False) -> List[Dict[str, Any]]:
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

        # Run scrapers concurrently using ThreadPoolExecutor
        def run_scraper(scraper_func, scraper_name):
            scraper_start = time.time()
            try:
                logging.debug(f"Starting {scraper_name} scraper")
                scraper_results = scraper_func(imdb_id, content_type, season, episode)
                if isinstance(scraper_results, tuple):
                    *_, scraper_results = scraper_results
                for item in scraper_results:
                    item['scraper'] = scraper_name
                    item['title'] = normalize_title(item.get('title', ''))  # Add normalized title
                logging.debug(f"{scraper_name} scraper found {len(scraper_results)} results")
                logging.debug(f"{scraper_name} scraper took {time.time() - scraper_start:.2f} seconds")
                return scraper_results
            except Exception as e:
                logging.error(f"Error in {scraper_name} scraper: {str(e)}")
                return []

        # Define scrapers
        all_scrapers = [
            (scrape_zilean, 'Zilean'),
            #(scrape_knightcrawler, 'Knightcrawler'),
            (scrape_torrentio, 'Torrentio'),
            (scrape_comet, 'Comet'),
            (scrape_jackett, 'Jackett'),
            (scrape_prowlarr, 'Prowlarr')
        ]

        scraping_start = time.time()
        with ThreadPoolExecutor(max_workers=len(all_scrapers)) as executor:
            future_to_scraper = {executor.submit(run_scraper, scraper_func, scraper_name): scraper_name for scraper_func, scraper_name in all_scrapers}
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
        filtered_results = filter_results(all_results, tmdb_id, title, year, content_type, season, episode, multi, version_settings, runtime, episode_count, season_episode_counts)
        logging.debug(f"Filtering took {time.time() - scraping_start:.2f} seconds")
        logging.debug(f"Total results after filtering: {len(filtered_results)}")

        # Add is_multi_pack information to each result
        for result in filtered_results:
            torrent_title = result.get('title', '')
            preprocessed_title = preprocess_title(torrent_title)
            season_pack = detect_season_pack(preprocessed_title)
            is_multi_pack = season_pack != 'N/A' and season_pack != 'Unknown'
            result['is_multi_pack'] = is_multi_pack
            result['season_pack'] = season_pack

            torrent_title = result.get('title', '')
            season_pack = result.get('season_pack', 'Unknown')
            if season_pack != 'Unknown' and season_pack != 'N/A':
                if season_pack == 'Complete':
                    logging.debug(f"Multi-episode result detected: {torrent_title} (Complete series)")
                else:
                    seasons = [int(s) for s in season_pack.split(',')]
                    if len(seasons) == 1:
                        logging.debug(f"Multi-episode result detected: {torrent_title} (Season: {seasons[0]})")
                    else:
                        start_season, end_season = min(seasons), max(seasons)
                        if start_season == end_season:
                            logging.debug(f"Multi-episode result detected: {torrent_title} (Season: {start_season})")
                        elif list(range(start_season, end_season + 1)) == seasons:
                            logging.debug(f"Multi-episode result detected: {torrent_title} (Seasons: {start_season} through {end_season})")
                        else:
                            logging.debug(f"Multi-episode result detected: {torrent_title} (Seasons: {', '.join(map(str, seasons))})")
            else:
                logging.debug(f"Single episode or movie result: {torrent_title}")

            # Debug logging for multi-episode results
            #if is_multi_pack:
                #if season_pack == 'Complete':
                    #logging.debug(f"Multi-episode result detected: {torrent_title} (Complete series)")
                #else:
                    #seasons = season_pack.split(',')
                    #num_seasons = len(seasons)
                    #logging.debug(f"Multi-episode result detected: {torrent_title} (Seasons: {season_pack}, Count: {num_seasons})")
            #else:
                #logging.debug(f"Single episode or movie result: {torrent_title}")

        # Sort results
        sorting_start = time.time()

        def stable_rank_key(x):
            # First, use the rank_result_key function with content_type and version_settings
            primary_key = rank_result_key(x, filtered_results, title, year, season, episode, multi, content_type, version_settings)

            # Then, use a tuple of stable secondary keys
            secondary_keys = (
                x.get('scraper', ''),  # Scraper name
                x.get('title', ''),    # Torrent title
                x.get('size', 0),      # Size
                x.get('seeders', 0)    # Seeders
            )

            return (primary_key, secondary_keys)

        final_results = sorted(filtered_results, key=stable_rank_key)
        logging.debug(f"Sorting took {time.time() - sorting_start:.2f} seconds")

        logging.debug(f"Total results in final output: {len(final_results)}")
        logging.debug(f"Total scraping process took {time.time() - start_time:.2f} seconds")

        # Debug printing for top 5 results
        #logging.debug("Top 5 results details:")
        #for i, result in enumerate(final_results[:5], 1):
            #logging.debug(f"Rank {i}:")
            #result_info = {
                #'title': result.get('title', ''),
                #'scraper': result.get('scraper', ''),
                #'size': result.get('size', ''),
                #'bitrate': result.get('bitrate', ''),
                #'seeders': result.get('seeders', 0),
                #'score': result.get('score_breakdown', {}).get('total_score', 0),
                #'season_pack': result.get('season_pack', 'Unknown'),
                #'is_multi_pack': result.get('is_multi_pack', False)
            #}
            #logging.debug(pformat(result_info, indent=2, width=120))
            #logging.debug("-" * 80)  # Separator between results

        return final_results

    except Exception as e:
        logging.error(f"Unexpected error in scrape function for {title} ({year}): {str(e)}", exc_info=True)
        return []
