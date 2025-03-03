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
    country_weight = int(version_settings.get('country_weight', 3))

    # Calculate base scores
    title_similarity = similarity(extracted_title, query)
    resolution_score = parsed_info.get('resolution_rank', 0)
    hdr_score = 1 if parsed_info.get('is_hdr', False) and version_settings.get('enable_hdr', True) else 0

    # Calculate country score
    media_country = result.get('media_country_code')  # This should be passed from the scrape function
    result_country = parsed_info.get('country')
    
    # Additional country code parsing for formats like "Title AU" or "Title NZ"
    if not result_country:
        # Common two-letter country codes that might appear after the title
        country_codes = {'AU': 'au', 'NZ': 'nz', 'UK': 'gb', 'US': 'us', 'CA': 'ca'}
        title_parts = torrent_title.split()
        for i, part in enumerate(title_parts):
            if part.upper() in country_codes and (i > 0 and not part.lower() in title_parts[i-1].lower()):  # Avoid matching part of a word
                result_country = country_codes[part.upper()]
                logging.info(f"Found country code in title: {part.upper()} -> {result_country}")
                break
    
    country_score = 0
    country_reason = "No country code matching applied"
    
    if media_country:
        if media_country.lower() == 'us':
            # For US content, treat missing country code as matching
            if not result_country:
                country_score = 10
                country_reason = "US content - implicit match (no country code)"
            elif result_country.lower() == 'us':
                country_score = 10
                country_reason = "US content - explicit match"
            else:
                country_score = -5
                country_reason = f"US content - non-matching country ({result_country})"
        else:
            # For non-US content, require explicit country match
            if result_country and media_country.lower() == result_country.lower():
                country_score = 10
                country_reason = f"Non-US content - explicit match ({media_country})"
            elif result_country:
                country_score = -5
                country_reason = f"Non-US content - wrong country (wanted {media_country}, got {result_country})"
            else:
                country_reason = f"Non-US content - no country code in result (wanted {media_country})"

    # Handle the case where torrent_year might be a list
    if query_year is None:
        year_match = 0  # No year match if query_year is None
        year_reason = "No query year provided"
    elif isinstance(torrent_year, list):
        if query_year in torrent_year:
            year_match = 10
            year_reason = f"Exact year match found in list: {query_year} in {torrent_year}"
        elif any(abs(query_year - y) <= 1 for y in torrent_year):
            year_match = 5
            year_reason = f"Year within 1 year difference in list: {query_year} near {torrent_year}"
        else:
            year_match = -5
            year_reason = f"Year mismatch penalty: {query_year} not near {torrent_year}"
    else:
        if query_year == torrent_year:
            year_match = 10
            year_reason = f"Exact year match: {query_year}"
        elif torrent_year and abs(query_year - (torrent_year or 0)) <= 1:
            year_match = 5
            year_reason = f"Year within 1 year difference: {query_year} vs {torrent_year}"
        elif torrent_year:
            year_match = -5
            year_reason = f"Year mismatch penalty: {query_year} vs {torrent_year}"
        else:
            year_match = 0
            year_reason = f"No year match: {query_year} vs {torrent_year}"

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
    normalized_country = country_score  # Already in 0-10 range

    # Apply weights
    weighted_similarity = normalized_similarity * similarity_weight
    weighted_resolution = normalized_resolution * resolution_weight
    weighted_hdr = normalized_hdr * hdr_weight
    weighted_size = normalized_size * size_weight
    weighted_bitrate = normalized_bitrate * bitrate_weight
    weighted_country = normalized_country * country_weight

    # Only apply season and episode matching for TV shows
    if content_type.lower() == 'episode':
        # Check if this is an anime
        genres = result.get('genres', [])
        if isinstance(genres, str):
            genres = [genres]
        is_anime = any('anime' in genre.lower() for genre in genres)
        
        if is_anime:
            # For anime, only match episode numbers, ignore season mismatch
            episode_match = 5 if query_episode == torrent_episode else 0
            season_match = 5  # Always give full season match score for anime
            logging.debug(f"Anime content - ignoring season mismatch. Episode match: {episode_match}")
        else:
            # Regular TV show matching
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
        SINGLE_EPISODE_PENALTY = -500
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
        weighted_country +
        (year_match * 5) +
        (season_match * 5) +
        (episode_match * 5) +
        multi_pack_score +
        single_episode_score +
        preferred_filter_score
    )

    # Content type matching score
    content_type_score = 0
    if content_type.lower() == 'movie' and not result.get('is_anime', False):
        if re.search(r'(s\d{2}|e\d{2}|season|episode)', torrent_title, re.IGNORECASE):
            content_type_score = -500
            logging.debug(f"Applied penalty for movie with season/episode in title")
    elif content_type.lower() == 'episode':
        # Use is_anime flag directly from result
        is_anime = result.get('is_anime', False)
        
        if not is_anime:
            # Regular TV show pattern matching
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
                        logging.debug(f"Applied penalty for TV show with year range in title")
                else:
                    content_type_score = -500
                    logging.debug(f"Applied penalty for TV show with no season/episode in title")
        else:
            # Anime pattern matching - look for episode number patterns like "Title - 20" or "Title 20"
            anime_format = result.get('anime_format')
            
            # Check for common anime episode patterns
            anime_episode_pattern = re.search(r'[-\s_](\d{1,3})(\s|$|\.|_|\[)', torrent_title)
            
            if anime_episode_pattern:
                # Found a potential episode number
                potential_episode = int(anime_episode_pattern.group(1))
                
                # Check if this matches our expected episode
                if potential_episode == query_episode:
                    content_type_score = 50  # Bonus for exact episode match
                    logging.debug(f"Applied bonus for anime with matching episode number: {potential_episode}")
                else:
                    content_type_score = -250  # Smaller penalty for wrong episode
                    logging.debug(f"Applied penalty for anime with wrong episode number: {potential_episode} (expected {query_episode})")
            elif anime_format:
                # If we have an anime_format but couldn't find an episode pattern, don't penalize
                # This could be a batch or other special format
                content_type_score = 0
                logging.debug(f"No episode pattern found but has anime_format: {anime_format}")
            else:
                # Try alternative pattern matching for anime batches
                batch_pattern = re.search(r'batch|season|complete|\(s\d+\)|\[s\d+\]', torrent_title, re.IGNORECASE)
                if batch_pattern:
                    # This is likely a batch/season pack
                    if multi:
                        content_type_score = 30  # Bonus for batch when multi is requested
                        logging.debug(f"Applied bonus for anime batch/season pack")
                    else:
                        content_type_score = -400  # Stronger penalty for batch when single episode requested
                        logging.debug(f"Applied strong penalty for anime batch when single episode requested")
                else:
                    # No clear episode indicators for anime
                    content_type_score = -250  # Smaller penalty for anime
                    logging.debug(f"Applied penalty for anime with no clear episode number")
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
        'country_score': round(weighted_country, 2),
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
            'bitrate': bitrate_weight,
            'country': country_weight
        },
        'min_size_gb': version_settings.get('min_size_gb', 0.01)
    }

    # Add the score breakdown to the result
    result['score_breakdown'] = score_breakdown

    # Log detailed score breakdown
    logging.debug(f"Score breakdown for '{torrent_title}':")
    logging.debug(f"├─ Title Similarity: {score_breakdown['similarity_score']:.2f} (weight: {similarity_weight})")
    logging.debug(f"├─ Resolution: {score_breakdown['resolution_score']:.2f} (weight: {resolution_weight})")
    logging.debug(f"├─ HDR: {score_breakdown['hdr_score']:.2f} (weight: {hdr_weight})")
    logging.debug(f"├─ Size: {score_breakdown['size_score']:.2f} (weight: {size_weight})")
    logging.debug(f"├─ Bitrate: {score_breakdown['bitrate_score']:.2f} (weight: {bitrate_weight})")
    logging.debug(f"├─ Country: {score_breakdown['country_score']:.2f} (weight: {country_weight}, reason: {country_reason})")
    logging.debug(f"├─ Year: {score_breakdown['year_match']:.2f} ({year_reason})")
    if content_type.lower() == 'episode':
        logging.debug(f"├─ Season Match: {score_breakdown['season_match']:.2f}")
        logging.debug(f"├─ Episode Match: {score_breakdown['episode_match']:.2f}")
        if score_breakdown['is_multi_pack']:
            logging.debug(f"├─ Multi-pack: {score_breakdown['multi_pack_score']:.2f} ({score_breakdown['num_items']} items)")
        if score_breakdown['single_episode_score']:
            logging.debug(f"├─ Single Episode Penalty: {score_breakdown['single_episode_score']:.2f}")
    if score_breakdown['content_type_score']:
        logging.debug(f"├─ Content Type Score: {score_breakdown['content_type_score']:.2f}")
    if preferred_filter_in_breakdown:
        logging.debug("├─ Preferred Filter Bonuses:")
        for pattern, score in preferred_filter_in_breakdown.items():
            logging.debug(f"│  ├─ {pattern}: +{score}")
    if preferred_filter_out_breakdown:
        logging.debug("├─ Preferred Filter Penalties:")
        for pattern, score in preferred_filter_out_breakdown.items():
            logging.debug(f"│  ├─ {pattern}: {score}")
    logging.debug(f"└─ Total Score: {score_breakdown['total_score']:.2f}")

    # Return negative total_score to sort in descending order
    return (-total_score, -year_match, -season_match, -episode_match)