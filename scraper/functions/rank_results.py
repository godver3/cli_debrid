import logging
import re
from typing import List, Dict, Any, Tuple
from scraper.functions.similarity_checks import similarity, normalize_title
from scraper.functions.other_functions import smart_search
from fuzzywuzzy import fuzz

def rank_result_key(
    result: Dict[str, Any], all_results: List[Dict[str, Any]],
    query: str, query_year: int, query_season: int, query_episode: int,
    multi: bool, content_type: str, version_settings: Dict[str, Any],
    preferred_language: str = None,
    translated_title: str = None
) -> Tuple:
    torrent_title = result.get('title', '')
    parsed_info = result.get('parsed_info', {})
    additional_metadata = result.get('additional_metadata', {}) # Get additional metadata

    extracted_title = parsed_info.get('title', torrent_title)
    filename = additional_metadata.get('filename') # Get filename
    binge_group = additional_metadata.get('bingeGroup') # Get bingeGroup
    torrent_year = parsed_info.get('year')
    torrent_season, torrent_episode = parsed_info.get('season'), parsed_info.get('episode')

    # Get user-defined weights
    resolution_weight = float(version_settings.get('resolution_weight', 3.0))
    hdr_weight = float(version_settings.get('hdr_weight', 3.0))
    similarity_weight = float(version_settings.get('similarity_weight', 3.0))
    size_weight = float(version_settings.get('size_weight', 3.0))
    bitrate_weight = float(version_settings.get('bitrate_weight', 3.0))
    country_weight = float(version_settings.get('country_weight', 3.0))
    language_weight = float(version_settings.get('language_weight', 3.0))
    year_match_weight = float(version_settings.get('year_match_weight', 3.0)) # New weight

    # Calculate base scores
    normalized_query = normalize_title(query).lower()
    normalized_extracted_title = normalize_title(extracted_title).lower()
    normalized_filename = normalize_title(filename).lower() if filename else None
    # Exclude bingeGroup from fuzzy matching unless format is reliable

    sim_extracted = fuzz.ratio(normalized_extracted_title, normalized_query) / 100.0
    sim_filename = fuzz.ratio(normalized_filename, normalized_query) / 100.0 if normalized_filename else 0.0

    title_similarity = max(sim_extracted, sim_filename) # Take the best similarity score
    
    # Handle resolution scoring with unknown resolution support
    resolution_score = parsed_info.get('resolution_rank', 0)
    if resolution_score == 0:
        # If resolution rank is 0, check if it's an unknown resolution
        resolution = parsed_info.get('resolution', '').lower()
        if resolution == 'unknown':
            # For unknown resolutions in older content/WEBRips, assign a low but non-zero score
            # This matches our filter behavior of treating unknown as SD/480p
            resolution_score = 1  # Equivalent to SD/480p ranking
            # logging.debug(f"Assigned resolution score of 1 (SD/480p) for unknown resolution in: {torrent_title}")
    
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
    year_match = 0 # Base score for year match
    year_reason = "Initialization"
    if query_year is None:
        year_match = 0  # No year match if query_year is None
        year_reason = "No query year provided"
    elif isinstance(torrent_year, list):
        # Ensure all elements in the list are integers before comparison
        int_torrent_years = [y for y in torrent_year if isinstance(y, int)]
        if not int_torrent_years:
             year_match = 0
             year_reason = f"No valid integer years found in list: {torrent_year}"
        elif query_year in int_torrent_years:
            year_match = 5 # Exact match score
            year_reason = f"Exact year match found in list: {query_year} in {int_torrent_years}"
        elif any(abs(query_year - y) <= 1 for y in int_torrent_years):
            year_match = 2.5 # Near match score
            year_reason = f"Year within 1 year difference in list: {query_year} near {int_torrent_years}"
        else:
            year_match = -5 # Mismatch penalty
            year_reason = f"Year mismatch penalty: {query_year} not near {int_torrent_years}"
    else: # Handles single int or None for torrent_year
        if query_year == torrent_year:
            year_match = 5 # Exact match score
            year_reason = f"Exact year match: {query_year}"
        elif torrent_year is not None and abs(query_year - torrent_year) <= 1:
            year_match = 2.5 # Near match score
            year_reason = f"Year within 1 year difference: {query_year} vs {torrent_year}"
        elif torrent_year is not None: # Mismatch but torrent has a year
            year_match = -5 # Mismatch penalty
            year_reason = f"Year mismatch penalty: {query_year} vs {torrent_year}"
        else: # Torrent has no year (torrent_year is None)
            year_match = 0
            year_reason = f"No year match (torrent has no year): {query_year} vs None"

    scraper = result.get('scraper', '').lower()

    # --- Size and Bitrate Calculation ---
    
    # Determine if this result is a multi-episode pack
    is_multi_pack = False
    season_pack = 'N/A' # Default
    num_items = 1 # Default
    if content_type.lower() == 'episode':
        season_pack = result.get('parsed_info', {}).get('season_episode_info', {}).get('season_pack', 'Unknown')
        is_multi_pack = season_pack not in ['N/A', 'Unknown'] or len(result.get('parsed_info', {}).get('season_episode_info', {}).get('episodes', [])) > 1
        if is_multi_pack:
            if season_pack == 'Complete':
                # Use a fixed moderate value instead of huge count
                num_items = 10 # Represents 'many' seasons
            elif season_pack not in ['N/A', 'Unknown']:
                 try:
                    num_items = len([s for s in season_pack.split(',') if s.isdigit()])
                 except:
                    num_items = 1 # Fallback
            else: # Unknown pack type (e.g., multiple episodes detected)
                 num_items = len(result.get('parsed_info', {}).get('season_episode_info', {}).get('episodes', []))
                 
    # Get the appropriate size for comparison based on 'multi' flag
    def get_comparison_size(r, is_multi_search):
        size_val = r.get('size', 0.0) # Default to 'size' (total size usually)
        sp_season = r.get('size_per_season')
        r_is_pack = False
        if r.get('media_type', content_type.lower()) == 'episode': # Check result type if available
             r_season_pack = r.get('parsed_info', {}).get('season_episode_info', {}).get('season_pack', 'Unknown')
             r_is_pack = r_season_pack not in ['N/A', 'Unknown'] or len(r.get('parsed_info', {}).get('season_episode_info', {}).get('episodes', [])) > 1
             
        if is_multi_search and r_is_pack and sp_season is not None:
            # Use per-season size for packs when doing a multi search
            return float(sp_season)
        elif not is_multi_search and r_is_pack:
             # Use total size for packs when doing a single search (packs are penalized later anyway)
             return float(r.get('total_size_gb', size_val))
        else:
            # Use standard size for movies or single episodes
             return float(r.get('total_size_gb', size_val)) # Prefer total_size_gb if available for consistency

    comparison_size = get_comparison_size(result, multi)
    bitrate = float(result.get('bitrate', 0)) # Use the already calculated overall bitrate

    # Calculate percentile ranks using the appropriate size metric
    all_comparison_sizes = [get_comparison_size(r, multi) for r in all_results]
    all_bitrates = [float(r.get('bitrate', 0)) for r in all_results]

    def percentile_rank(value, all_values):
        # Filter out zero or invalid values before calculating percentile
        valid_values = [v for v in all_values if v is not None and v > 0]
        if not valid_values:
            return 0 # Avoid division by zero if no valid values
        return sum(1 for v in valid_values if v <= value) / len(valid_values) if value > 0 else 0

    size_percentile = percentile_rank(comparison_size, all_comparison_sizes)
    bitrate_percentile = percentile_rank(bitrate, all_bitrates)
    # --- End Size and Bitrate Calculation ---

    # Normalize scores (most to 0-10 range, resolution uses its own scale)
    normalized_similarity = title_similarity * 10
    normalized_resolution = resolution_score * 5 # Higher multiplier emphasizes resolution
    normalized_hdr = hdr_score * 10
    normalized_size = size_percentile * 10
    normalized_bitrate = bitrate_percentile * 10
    normalized_country = country_score  # Already in +/-10 range

    # Calculate language score based on preferred language
    # Simplified logic: Score based ONLY on similarity to translated title
    language_score = 0
    language_reason = "No language preference or no translated title available"
    if translated_title:
        parsed_title = parsed_info.get('title', '')
        normalized_parsed_title = normalize_title(parsed_title).lower()
        normalized_translated_q_title = normalize_title(translated_title).lower()
        title_lang_sim = fuzz.ratio(normalized_parsed_title, normalized_translated_q_title) / 100.0

        # Score based on similarity threshold
        similarity_threshold = 0.90 # High threshold for strong match
        if title_lang_sim >= similarity_threshold:
            language_score = 100 # Bonus for strong match to translation
            language_reason = f"High similarity to translated title ({title_lang_sim:.2f} >= {similarity_threshold})"
        else:
            language_score = -25 # Penalty for not matching translation
            language_reason = f"Low similarity to translated title ({title_lang_sim:.2f} < {similarity_threshold})"

    normalized_language = language_score # Use the raw score

    # Apply weights
    weighted_similarity = normalized_similarity * similarity_weight
    weighted_resolution = normalized_resolution * resolution_weight
    weighted_hdr = normalized_hdr * hdr_weight
    weighted_size = normalized_size * size_weight
    weighted_bitrate = normalized_bitrate * bitrate_weight
    weighted_country = normalized_country * country_weight
    weighted_language = normalized_language * language_weight
    weighted_year_match = year_match * year_match_weight # Apply new weight to year score

    # Only apply season and episode matching for TV shows
    season_match_score = 0
    episode_match_score = 0
    if content_type.lower() == 'episode':
        # Check if this is an anime
        genres = result.get('genres', [])
        if isinstance(genres, str):
            genres = [genres]
        is_anime = any('anime' in genre.lower() for genre in genres)
        
        if is_anime:
            # For anime, only match episode numbers, ignore season mismatch
            episode_match_score = 5 if query_episode == torrent_episode else 0
            season_match_score = 5  # Always give full season match score for anime
            # logging.debug(f"Anime content - ignoring season mismatch. Episode match: {episode_match_score}")
        else:
            # Regular TV show matching
            season_match_score = 5 if query_season == torrent_season else 0
            episode_match_score = 5 if query_episode == torrent_episode else 0
    else:
        season_match_score = 0
        episode_match_score = 0

    # Multi-pack handling (only for TV shows)
    multi_pack_score = 0
    single_episode_score = 0
    if content_type.lower() == 'episode':
        # is_multi_pack and season_pack calculated earlier
        
        # Check if the pack contains the queried season
        is_queried_season_pack = False
        if is_multi_pack:
             if season_pack == 'Complete':
                  # Assume complete pack contains the queried season
                  is_queried_season_pack = True
             elif season_pack not in ['N/A', 'Unknown']:
                  is_queried_season_pack = str(query_season) in [s for s in season_pack.split(',') if s.isdigit()]

        # Apply a FLAT bonus for multi-packs when requested and correct season found
        FLAT_PACK_BONUS = 30 # Tunable flat bonus value
        multi_pack_score = FLAT_PACK_BONUS if multi and is_multi_pack and is_queried_season_pack else 0

        # Penalize multi-packs when looking for single episodes
        SINGLE_EPISODE_PENALTY = -500
        # Apply penalty if not multi search AND it's a pack (use is_multi_pack flag)
        single_episode_score = SINGLE_EPISODE_PENALTY if not multi and is_multi_pack else 0

    # Implement preferred filtering logic
    preferred_filter_score = 0
    torrent_title_lower = torrent_title.lower()
    filename_lower = filename.lower() if filename else None
    binge_group_lower = binge_group.lower() if binge_group else None
    fields_to_check_pref = [torrent_title_lower, filename_lower, binge_group_lower]

    # Function to check preferred patterns against multiple fields
    def check_preferred(patterns_weights, fields, is_bonus):
        score_change = 0
        breakdown = {}
        for pattern, weight in patterns_weights:
            pattern_matched = False
            for field_value in fields:
                if field_value and smart_search(pattern, field_value):
                    score_change += weight if is_bonus else -weight
                    breakdown[pattern] = weight if is_bonus else -weight
                    pattern_matched = True
                    break # Apply weight only once per pattern
            # if pattern_matched: # Log which pattern matched which field (optional)
            #      logging.debug(f"Pref Filter {'Bonus' if is_bonus else 'Penalty'}: Pattern '{pattern}' matched.")

        return score_change, breakdown

    # Apply preferred_filter_in bonus
    in_score, in_breakdown = check_preferred(version_settings.get('preferred_filter_in', []), fields_to_check_pref, is_bonus=True)
    preferred_filter_score += in_score
    preferred_filter_in_breakdown = in_breakdown

    # Apply preferred_filter_out penalty
    out_score, out_breakdown = check_preferred(version_settings.get('preferred_filter_out', []), fields_to_check_pref, is_bonus=False)
    preferred_filter_score += out_score # Remember out_score is already negative if matched
    preferred_filter_out_breakdown = out_breakdown

    # Combine scores
    total_score = (
        weighted_similarity +
        weighted_resolution +
        weighted_hdr +
        weighted_size +
        weighted_bitrate +
        weighted_country +
        weighted_language +
        weighted_year_match + # Use weighted year score
        (season_match_score * 5) + # Use base season score * multiplier
        (episode_match_score * 5) + # Use base episode score * multiplier
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
        'language_score': round(weighted_language, 2),
        'year_match_score': round(weighted_year_match, 2), # Store weighted year score
        'season_match_score': round(season_match_score * 5, 2), # Store calculated season score
        'episode_match_score': round(episode_match_score * 5, 2), # Store calculated episode score
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
            'country': country_weight,
            'language': language_weight,
            'year': year_match_weight # Add year weight here
        },
        'min_size_gb': version_settings.get('min_size_gb', 0.01)
    }

    # Add the score breakdown to the result
    result['score_breakdown'] = score_breakdown

    # Log detailed score breakdown
    # logging.debug(f"Score breakdown for '{torrent_title}':")
    # logging.debug(f"├─ Title Similarity: {score_breakdown['similarity_score']:.2f} (weight: {similarity_weight})")
    # logging.debug(f"├─ Resolution: {score_breakdown['resolution_score']:.2f} (weight: {resolution_weight})")
    # logging.debug(f"├─ HDR: {score_breakdown['hdr_score']:.2f} (weight: {hdr_weight})")
    # logging.debug(f"├─ Size: {score_breakdown['size_score']:.2f} (weight: {size_weight})")
    # logging.debug(f"├─ Bitrate: {score_breakdown['bitrate_score']:.2f} (weight: {bitrate_weight})")
    # logging.debug(f"├─ Country: {score_breakdown['country_score']:.2f} (weight: {country_weight}, reason: {country_reason})")
    # logging.debug(f"├─ Language: {score_breakdown['language_score']:.2f} (weight: {language_weight}, reason: {language_reason})")
    # logging.debug(f"├─ Year Match: {score_breakdown['year_match_score']:.2f} (base: {year_match}, weight: {year_match_weight}, reason: {year_reason})")
    # if content_type.lower() == 'episode':
    #     logging.debug(f"├─ Season Match: {score_breakdown['season_match_score']:.2f} (base: {season_match_score})")
    #     logging.debug(f"├─ Episode Match: {score_breakdown['episode_match_score']:.2f} (base: {episode_match_score})")
    #     if score_breakdown['is_multi_pack']:
    #         logging.debug(f"├─ Multi-pack: {score_breakdown['multi_pack_score']:.2f} ({score_breakdown['num_items']} items)")
    #     if score_breakdown['single_episode_score']:
    #         logging.debug(f"├─ Single Episode Penalty: {score_breakdown['single_episode_score']:.2f}")
    # if score_breakdown['content_type_score']:
    #     logging.debug(f"├─ Content Type Score: {score_breakdown['content_type_score']:.2f}")
    # if preferred_filter_in_breakdown:
    #     logging.debug("├─ Preferred Filter Bonuses:")
    #     for pattern, score in preferred_filter_in_breakdown.items():
    #         logging.debug(f"│  ├─ {pattern}: +{score}")
    # if preferred_filter_out_breakdown:
    #     logging.debug("├─ Preferred Filter Penalties:")
    #     for pattern, score in preferred_filter_out_breakdown.items():
    #         logging.debug(f"│  ├─ {pattern}: {score}")
    # logging.debug(f"└─ Total Score: {score_breakdown['total_score']:.2f}")

    # Return negative total_score to sort in descending order
    # Year match raw score is used as a tie-breaker
    return (-total_score, -year_match, -season_match_score, -episode_match_score)