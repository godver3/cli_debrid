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

def detect_hdr(title: str) -> bool:
    hdr_terms = ['HDR', 'DV', 'DOVI', 'DOLBY VISION', 'DOLBY.VISION', 'HDR10+', 'HDR10plus', 'HDR10', 'HLG']
    return any(term in title.upper() for term in hdr_terms)

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

def calculate_bitrate(size_gb, runtime_minutes):
    if not size_gb or not runtime_minutes:
        return 0
    size_bits = size_gb * 8 * 1024 * 1024 * 1024 * 100  # Convert GB to bits
    runtime_seconds = runtime_minutes * 60
    bitrate_mbps = (size_bits / runtime_seconds) / 1000000  # Convert to Mbps
    return round(bitrate_mbps, 2)

import re
import logging

def detect_resolution(title: str) -> str:
    logging.debug(f"Detecting resolution for title: '{title}'")
    resolution_patterns = [
        (r'(?i)(?:^|\D)(2160p|4k|uhd)(?:$|\D)', '2160p'),
        (r'(?i)(?:^|\D)(1080p|fhd)(?:$|\D)', '1080p'),
        (r'(?i)(?:^|\D)(720p|hd)(?:$|\D)', '720p'),
        (r'(?i)(?:^|\D)(480p|sd)(?:$|\D)', '480p'),
    ]
    title_lower = title.lower()
    
    # First, try to match the exact patterns
    for pattern, res in resolution_patterns:
        match = re.search(pattern, title_lower)
        if match:
            logging.debug(f"Matched exact pattern '{pattern}' in title. Detected resolution: {res}")
            return res
    logging.debug("No exact pattern match found.")
    
    # If no exact match, look for numbers followed by 'p'
    p_match = re.search(r'(\d+)p', title_lower)
    if p_match:
        p_value = int(p_match.group(1))
        logging.debug(f"Found '{p_value}p' in title.")
        if p_value >= 2160:
            logging.debug("Categorized as 2160p")
            return '2160p'
        elif p_value >= 1080:
            logging.debug("Categorized as 1080p")
            return '1080p'
        elif p_value >= 720:
            logging.debug("Categorized as 720p")
            return '720p'
        elif p_value >= 480:
            logging.debug("Categorized as 480p")
            return '480p'
    else:
        logging.debug("No 'Xp' pattern found in title.")
    
    # If still no match, look for '4k' or 'uhd'
    if '4k' in title_lower or 'uhd' in title_lower:
        logging.debug("Found '4k' or 'uhd' in title. Categorized as 2160p")
        return '2160p'
    
    logging.debug("No resolution detected. Returning 'unknown'")
    return 'unknown'

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
    # Regular expression to match season patterns
    season_patterns = [
        r'\bS(\d{1,2})(?:\s*-?\s*S?(\d{1,2}))?\b',  # Matches S01, S01-S03, S01 03, S01-03
        r'\bSeason\s+(\d{1,2})(?:\s*-?\s*(\d{1,2}))?\b',  # Matches Season 1, Season 1-3, Season 1 3
        r'\bSaison\s+(\d{1,2})(?:\s*-?\s*(\d{1,2}))?\b',  # Matches French "Saison 1", "Saison 1-3", "Saison 1 3"
        r'\bSeason(?:\s+\d{1,2}){2,}\b',  # Matches "Season 1 2 3 4 5" (at least two numbers)
        r'\b(?:Seasons|Season)\s+(\d{1,2})\s*-?\s*(\d{1,2})\b',  # Matches "Seasons 1-6", "Season 1-6"
    ]

    # Check for single episode pattern first
    episode_pattern = r'\bS(\d{1,2})E(\d{1,2})(?:-E?(\d{1,2}))?\b'
    episode_match = re.search(episode_pattern, title, re.IGNORECASE)
    if episode_match:
        season = int(episode_match.group(1))
        start_ep = int(episode_match.group(2))
        end_ep = int(episode_match.group(3)) if episode_match.group(3) else start_ep
        if end_ep - start_ep > 5:  # Assume it's a season pack if more than 5 episodes
            return str(season)
        return 'N/A'

    # Add a pattern for multi-episode releases within a single season
    multi_episode_pattern = r'\bS(\d{1,2})E(\d{1,2})-E(\d{1,2})\b'
    multi_episode_match = re.search(multi_episode_pattern, title, re.IGNORECASE)
    if multi_episode_match:
        season = int(multi_episode_match.group(1))
        start_ep = int(multi_episode_match.group(2))
        end_ep = int(multi_episode_match.group(3))
        if end_ep - start_ep > 1:  # It's a multi-episode release
            return f"{season}:M"  # Use a special notation for multi-episode

    # Check for the specific case mentioned
    if re.search(r'\b(?:Seasons|Season)\s+1-6\b', title, re.IGNORECASE):
        return '1,2,3,4,5,6'

    for pattern in season_patterns:
        match = re.search(pattern, title, re.IGNORECASE)
        if match:
            if 'Season' in pattern and '{2,}' in pattern:  # This is our pattern for "Season 1 2 3 4 5" format
                seasons = [int(s) for s in re.findall(r'\d+', match.group(0))]
                if len(seasons) > 1:  # Ensure we have at least two season numbers
                    return ','.join(str(s) for s in range(min(seasons), max(seasons) + 1))
            else:
                start_season = int(match.group(1))
                end_season = int(match.group(2)) if match.group(2) else start_season
                if end_season < start_season:
                    end_season, start_season = start_season, end_season
                # Sanity check: limit the maximum number of seasons
                if end_season > 50 or start_season > 50:
                    return 'Unknown'
                if start_season != end_season:
                    return ','.join(str(s) for s in range(start_season, end_season + 1))
                else:
                    return str(start_season)

    # Check for complete series or multiple seasons
    if re.search(r'\b(complete series|all seasons)\b', title, re.IGNORECASE):
        return 'Complete'

    # Check for specific ranges like "1-6" in the title
    range_match = re.search(r'\b(\d{1,2})\s*-\s*(\d{1,2})\b', title)
    if range_match:
        start, end = map(int, range_match.groups())
        if 1 <= start < end <= 50:  # Sanity check
            return ','.join(str(s) for s in range(start, end + 1))

    # If no clear pattern is found, return 'Unknown'
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
    size = parse_size(result.get('size', 0))
    runtime = result.get('runtime', 0)
    bitrate = result.get('bitrate', 0)  # Use pre-calculated bitrate

    # Calculate percentile ranks for size and bitrate
    all_sizes = [parse_size(r.get('size', 0)) for r in all_results]
    all_bitrates = [r.get('bitrate', 0) for r in all_results]  # Use pre-calculated bitrates

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
    is_queried_season_pack = is_multi_pack and (str(query_season) in season_pack.split(',') or ':M' in season_pack)

    # Calculate the number of seasons or episodes in the pack
    if is_multi_pack:
        if season_pack == 'Complete':
            num_items = 100  # Assign a high value for complete series
        elif ':M' in season_pack:  # Multi-episode release
            num_items = result.get('normalized_episode_count', 1)
        else:
            num_items = len(season_pack.split(','))
    else:
        num_items = 1

    # Apply a bonus for multi-packs when requested, scaled by the number of items
    MULTI_PACK_BONUS = 10  # Base bonus
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
        if re.search(r'(s\d{2}|e\d{2})', torrent_title, re.IGNORECASE):
            content_type_score = -500
    elif content_type.lower() == 'episode':
        if not re.search(r'(s\d{2}|e\d{2})', torrent_title, re.IGNORECASE):
            content_type_score = -500
        if re.search(r'\b(19|20)\d{2}\b', torrent_title) and not re.search(r'(season|episode|s\d{2}|e\d{2})', torrent_title, re.IGNORECASE):
            content_type_score -= 250
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

    logging.debug(f"Comparing resolutions: {res1} ({val1}) vs {res2} ({val2})")
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
            logging.debug(f"Existing: '{existing_result.get('title')}', New: '{title}'")
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

    logging.debug(f"Filtering settings: resolution_wanted={resolution_wanted}, max_resolution={max_resolution}, min_size_gb={min_size_gb}")

    def resolution_filter(result_resolution):
        if result_resolution == 'unknown':
            logging.debug(f"Unknown resolution found for result, allowing it to pass")
            return True
        logging.debug(f"Comparing resolutions: result={result_resolution}, max={max_resolution}")
        comparison = compare_resolutions(result_resolution, max_resolution)
        logging.debug(f"Resolution comparison result: {comparison}")
        if resolution_wanted == '<=':
            return comparison <= 0
        elif resolution_wanted == '==':
            return comparison == 0
        elif resolution_wanted == '>=':
            return comparison >= 0
        return True  # If the operator is invalid, allow the result to pass

    for result in results:
        original_title = result.get('title', '')
        
        # Detect resolution using the original title
        detected_resolution = detect_resolution(original_title)
        logging.debug(f"Detected resolution for '{original_title}': {detected_resolution}")
        
        # Now preprocess the title
        preprocessed_title = preprocess_title(original_title)
        
        # Parse the preprocessed title, but keep our detected resolution
        parsed_info = parse_torrent_info(preprocessed_title)
        parsed_info['resolution'] = detected_resolution  # Ensure we use our detected resolution

        logging.debug(f"Processing result: {original_title}")
        logging.debug(f"Parsed info: {parsed_info}")

        # Apply resolution filter
        if not resolution_filter(detected_resolution):
            logging.debug(f"Filtered out due to resolution mismatch: {detected_resolution}")
            continue

        # Apply HDR filter
        is_hdr = detect_hdr(original_title)
        if not enable_hdr and is_hdr:
            logging.debug(f"Filtered out HDR content when HDR is disabled: {original_title}")
            continue

        # Check title similarity
        parsed_title = parsed_info.get('title', '')
        title_sim = similarity(title, parsed_title)
        logging.debug(f"Title similarity: {title_sim} ('{title}' vs '{parsed_title}')")
        if title_sim < 0.8:
            logging.debug(f"Filtered out due to low title similarity: {title_sim}")
            continue

        # Content type specific filtering
        if content_type.lower() == 'movie':
            parsed_year = parsed_info.get('year')
            if not parsed_year:
                logging.debug(f"Filtered out due to missing year")
                continue
            if abs(int(parsed_year) - year) > 1:
                logging.debug(f"Filtered out due to year mismatch: {parsed_year} vs {year}")
                continue
        elif content_type.lower() == 'episode':
            if multi:
                if re.search(r'S\d{2}E\d{2}', original_title, re.IGNORECASE):
                    logging.debug(f"Filtered out single episode result when searching for multi")
                    continue
            else:
                result_season = parsed_info.get('season')
                result_episode = parsed_info.get('episode')
                if result_season != season or result_episode != episode:
                    logging.debug(f"Filtered out due to season/episode mismatch: S{result_season}E{result_episode} vs S{season}E{episode}")
                    continue

        # Apply size filter
        size_gb = parse_size(result.get('size', 0))
        if size_gb < min_size_gb:
            logging.debug(f"Filtered out due to small size: {size_gb} GB")
            continue

        # Apply custom filters with smart matching
        if filter_in and not any(smart_search(pattern, original_title) for pattern in filter_in):
            logging.debug(f"Filtered out due to not matching any filter_in patterns")
            continue
        if filter_out:
            matched_patterns = [pattern for pattern in filter_out if smart_search(pattern, original_title)]
            if matched_patterns:
                logging.debug(f"Filtered out due to matching filter_out pattern(s): {', '.join(matched_patterns)}")
                continue

        # Handle multi-episode releases
        season_pack = detect_season_pack(original_title)
        if multi:
            if season_pack == 'N/A' or season_pack == 'Unknown':
                logging.debug(f"Filtered out non-multi result when searching for multi")
                continue
            if ':M' in season_pack:  # It's a multi-episode release
                result_season = int(season_pack.split(':')[0])
                if result_season != season:
                    logging.debug(f"Filtered out multi-episode release for wrong season: {result_season} vs {season}")
                    continue
            elif str(season) not in season_pack.split(','):
                logging.debug(f"Filtered out season pack not containing the requested season")
                continue
        else:
            if season_pack != 'N/A' and season_pack != 'Unknown':
                logging.debug(f"Filtered out multi-episode release when searching for single episode")
                continue

        # Calculate normalized size and bitrate
        if content_type.lower() == 'episode':
            if season_pack != 'N/A' and season_pack != 'Unknown':
                if season_pack == 'Complete':
                    total_episodes = sum(season_episode_counts.values())
                else:
                    total_episodes = sum(season_episode_counts.get(int(s), 0) for s in season_pack.split(','))
            else:
                total_episodes = 1
            normalized_episode_count = max(1, total_episodes)  # Ensure we don't divide by zero
        else:
            normalized_episode_count = 1

        original_size_gb = size_gb
        size_gb /= normalized_episode_count
        bitrate = calculate_bitrate(size_gb, runtime)
        result['bitrate'] = bitrate

        # Add parsed info and other calculated fields to the result
        result['parsed_info'] = parsed_info
        result['normalized_episode_count'] = normalized_episode_count
        result['season_pack'] = season_pack
        result['title_similarity'] = title_sim
        result['is_hdr'] = is_hdr

        filtered_results.append(result)
        logging.debug(f"Result passed all filters: {original_title}")

    logging.debug(f"Filtering complete. {len(filtered_results)} results passed out of {len(results)} total")
    return filtered_results

def normalize_title(title: str) -> str:
    """
    Normalize the title by replacing spaces with periods and removing duplicate periods.
    """
    # Replace spaces with periods
    normalized = title.replace(' ', '.')
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

            # Debug logging for multi-episode results
            if is_multi_pack:
                if season_pack == 'Complete':
                    logging.debug(f"Multi-episode result detected: {torrent_title} (Complete series)")
                else:
                    seasons = season_pack.split(',')
                    num_seasons = len(seasons)
                    logging.debug(f"Multi-episode result detected: {torrent_title} (Seasons: {season_pack}, Count: {num_seasons})")
            else:
                logging.debug(f"Single episode or movie result: {torrent_title}")

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
        return final_results

    except Exception as e:
        logging.error(f"Unexpected error in scrape function for {title} ({year}): {str(e)}", exc_info=True)
        return []
