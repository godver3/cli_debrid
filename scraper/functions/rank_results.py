import logging
import re
from typing import List, Dict, Any, Tuple
from scraper.functions.similarity_checks import similarity
from scraper.functions.other_functions import smart_search

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