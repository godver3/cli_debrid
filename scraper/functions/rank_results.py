import logging
import re
from typing import List, Dict, Any, Tuple, Optional
from scraper.functions.similarity_checks import similarity, normalize_title
from scraper.functions.other_functions import smart_search
from fuzzywuzzy import fuzz

# Function to normalize filter patterns for tracking duplicates
def _normalize_filter_pattern(pattern: str) -> str:
    return re.sub(r'[\s-]+', '', pattern).lower()

# Modified check_preferred function
def check_preferred(patterns_weights, fields, is_bonus):
    score_change = 0
    breakdown = {}
    matched_normalized_patterns = set() # Track normalized forms matched for this item

    for pattern, weight in patterns_weights:
        normalized_pattern = _normalize_filter_pattern(pattern)

        # Check if this normalized form has already contributed to the score
        if normalized_pattern in matched_normalized_patterns:
            # logging.debug(f"Skipping pattern '{pattern}' as normalized '{normalized_pattern}' already scored.")
            continue # Skip if normalized form already scored

        # Check the pattern against the fields using smart_search
        for field_value in fields:
            # Ensure field_value is not None before lowercasing/checking
            field_str = field_value if isinstance(field_value, str) else ""
            if smart_search(pattern, field_str): # Use original pattern for smart_search
                # Matched! Apply score *once* for this normalized pattern
                score_change += weight if is_bonus else -weight
                breakdown[pattern] = weight if is_bonus else -weight # Record the original pattern
                matched_normalized_patterns.add(normalized_pattern) # Mark normalized form as scored
                # logging.debug(f"Pref Filter {'Bonus' if is_bonus else 'Penalty'}: Normalized '{normalized_pattern}' matched via pattern '{pattern}'. Applied score change: {weight if is_bonus else -weight}")
                break # Apply weight only once per pattern (stop checking fields for this pattern)

    return score_change, breakdown

def rank_result_key(
    result: Dict[str, Any], all_results: List[Dict[str, Any]],
    query: str, query_year: int, query_season: int, query_episode: int,
    multi: bool, content_type: str, version_settings: Dict[str, Any],
    preferred_language: str = None,
    translated_title: str = None,
    show_season_episode_counts: Optional[Dict[int, int]] = None
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
    title_similarity = max(sim_extracted, sim_filename)
    
    resolution_score = parsed_info.get('resolution_rank', 0)
    if resolution_score == 0:
        resolution = parsed_info.get('resolution', '').lower()
        if resolution == 'unknown':
            resolution_score = 1
    
    hdr_score = 1 if parsed_info.get('is_hdr', False) and version_settings.get('enable_hdr', True) else 0

    media_country = result.get('media_country_code')
    result_country = parsed_info.get('country')
    # PTT parser already provides country codes, no need for fallback detection
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

    year_match = 0 
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

    # Normalize base scores
    normalized_similarity = title_similarity * 10
    normalized_resolution = resolution_score * 50 
    normalized_hdr = hdr_score * 10
    # country_score is already in a good range, no specific normalization like *10 needed here
    # language_score will be calculated and normalized later

    # --- Determine if this result is a multi-episode pack and calculate accurate num_items ---
    # --- This block is now placed AFTER raw score calculation and initial normalization ---
    is_multi_pack = False
    season_pack_type = 'N/A' # Default
    num_items = 1 

    if content_type.lower() == 'episode':
        parsed_sei = parsed_info.get('season_episode_info', {})
        parsed_episodes_list = parsed_sei.get('episodes', [])
        parsed_seasons_list = parsed_sei.get('seasons', [])
        season_pack_type = parsed_sei.get('season_pack', 'Unknown') # This will be 'Complete'

        # Priority 1: Check for a "Complete Series" pack.
        # This is a "Complete Series" pack if `season_pack_type` is 'Complete' and it's not a single season pack
        # (i.e., parsed_seasons_list doesn't contain just one season).
        is_single_season_pack = len(parsed_seasons_list) == 1 and all(str(s).isdigit() for s in parsed_seasons_list)
        if season_pack_type == 'Complete' and not is_single_season_pack and show_season_episode_counts:
            # This logic is for a "Complete Series" pack.
            num_items = sum(count for s_num, count in show_season_episode_counts.items() if isinstance(s_num, int) and s_num > 0)
            is_multi_pack = num_items > 1
            logging.debug(f"[RRK] P1 (New): 'Complete Series' logic triggered. Num items: {num_items} for '{torrent_title}'")

        # Priority 2: Explicit single episode identified by parser (e.g., S01E01 via episodes list)
        elif parsed_episodes_list and len(parsed_episodes_list) == 1:
            num_items = 1
            is_multi_pack = False
            # logging.debug(f"[RRK] P2: Single episode (parsed_episodes_list len 1): {torrent_title}")

        # Priority 3: Multi-episode range (e.g., S01E01-E10 via episodes list)
        elif parsed_episodes_list and len(parsed_episodes_list) > 1:
            num_items = len(parsed_episodes_list)
            is_multi_pack = True
            # logging.debug(f"[RRK] P3: Multi-episode range ({num_items} items): {torrent_title}")

        # Priority 4: Specific season(s) pack (e.g., S01, S01-S03, or S03 COMPLETE if '03' is in parsed_seasons_list)
        elif parsed_seasons_list and show_season_episode_counts:
            current_total_episodes = 0
            for s_num_str in parsed_seasons_list: # parsed_seasons_list contains season numbers as strings
                try:
                    s_num_int = int(s_num_str)
                    if s_num_int > 0: # Ensure season number is valid positive integer
                        current_total_episodes += show_season_episode_counts.get(s_num_int, 0)
                except ValueError:
                    logging.warning(f"[RRK] P4: Could not convert season '{s_num_str}' to int for '{torrent_title}'")
            
            if current_total_episodes > 0:
                num_items = current_total_episodes
                is_multi_pack = True # A season pack is a multi-pack
                # logging.debug(f"[RRK] P4: Specific season(s) pack ({num_items} items from {parsed_seasons_list}): {torrent_title}")

        # Priority 5: Fallback 'Complete' pack (interpreted as whole show if NO specific seasons were identified)
        elif season_pack_type == 'Complete' and not parsed_seasons_list and show_season_episode_counts:
            num_items = sum(count for s_num, count in show_season_episode_counts.items() if isinstance(s_num, int) and s_num > 0)
            is_multi_pack = num_items > 1
            # logging.debug(f"[RRK] P5: 'Complete' (whole show - no specific seasons parsed) pack ({num_items} items): {torrent_title}")
        
        # Priority 6: Basic torrent_season and torrent_episode fields (if not already identified as multi-episode or specific pack)
        elif isinstance(torrent_season, int) and isinstance(torrent_episode, int) and not is_multi_pack:
            # logging.debug(f"[RRK] P6: Single episode (torrent_season/episode present, not a pack): {torrent_title}")
            pass # num_items = 1, is_multi_pack = False confirmed.

        # Fallback: If season_pack_type indicates a pack (e.g. "S01", "S01,S02", or even "Complete")
        # and num_items is still 1 (e.g. show_season_episode_counts was missing for specific seasons, or 'Complete' pack had 0/1 total episodes),
        # we should ensure is_multi_pack is true for scoring.
        if num_items <= 1 and season_pack_type not in ['N/A', 'Unknown'] and season_pack_type: # "Complete" is a valid pack type here
            is_multi_pack = True # Mark as pack for scoring, even if num_items remains 1 (due to missing counts).
            # logging.debug(f"[RRK] Fallback: Marked as multi-pack due to season_pack_type '{season_pack_type}' (num_items={num_items}): {torrent_title}")
        
        # Final safety check: if after all this, it's not a multi-pack but num_items > 1, log warning and fix.
        # Or, if it IS a multi-pack but num_items is 1 (and not due to fallback where counts are unknown), also suspicious.
        if not is_multi_pack and num_items > 1:
            logging.warning(f"[RRK] Contradiction: Not a multi-pack but num_items = {num_items} for '{torrent_title}'. Resetting num_items to 1.")
            num_items = 1
        elif is_multi_pack and num_items <= 1 and not (season_pack_type not in ['N/A', 'Unknown'] and season_pack_type):
             # This condition means it was marked is_multi_pack (e.g. by 'Complete' or episode range) but num_items is still 1
             # and it wasn't due to the specific fallback for season_pack_type (like "S01").
             # This could happen if a 'Complete' pack somehow has 0 or 1 episodes in show_season_episode_counts.
             # logging.debug(f"[RRK] Minor Contradiction: is_multi_pack is True but num_items = {num_items} for '{torrent_title}'. Retaining is_multi_pack for scoring.")
             pass # Allow is_multi_pack to be true even if num_items is 1, if it's a pack type.

    # --- Set comparison_size directly from result['size'] (calculated in filter_results.py) ---
    comparison_size = result.get('size', 0.0) 
    
    bitrate = float(result.get('bitrate', 0)) # Use the already calculated overall bitrate

    # --- New Absolute Normalized Size Score (uses the direct comparison_size) ---
    min_s_setting = version_settings.get('min_size_gb', 0.01)
    min_s = float(min_s_setting if min_s_setting is not None else 0.01)
    
    max_s_setting = version_settings.get('max_size_gb', float('inf'))
    max_s = float(max_s_setting if max_s_setting is not None else float('inf'))

    current_s = comparison_size # current_s is now always the (average) per-item size

    normalized_size_factor = 0.0
    normalized_size = 0.0
    if current_s >= min_s:
        if max_s != float('inf') and max_s > min_s: # User has defined a specific max per-item size
            normalized_size_factor = (current_s - min_s) / (max_s - min_s)
        else: # max_s is infinity or not sensible, use reference caps for per-item size
            reference_cap_s = 0.0
            if content_type.lower() == 'episode':
                # comparison_size is per-episode (or average per-episode for packs)
                reference_cap_s = 3.0 # Reference cap for a single episode's size
            else: # movie
                reference_cap_s = 30.0 # Reference cap for a movie's size
            
            # Ensure reference_cap_s is greater than min_s for sensible scaling
            if reference_cap_s <= min_s: 
                reference_cap_s = min_s * 2.0 if min_s > 0 else 1.0 # Avoid 0 or negative range
            
            if reference_cap_s > min_s : 
                 normalized_size_factor = (current_s - min_s) / (reference_cap_s - min_s)
            else: 
                 normalized_size_factor = 0.0

        normalized_size_factor = max(0.0, min(normalized_size_factor, 1.0)) # Clamp to 0-1
        normalized_size = (normalized_size_factor ** 1.5) * 20.0 
    
    # --- New Absolute Normalized Bitrate Score ---
    min_b_mbps_setting = version_settings.get('min_bitrate_mbps', 0.0)
    min_b_mbps = float(min_b_mbps_setting if min_b_mbps_setting is not None else 0.0)

    max_b_mbps_setting = version_settings.get('max_bitrate_mbps', float('inf'))
    max_b_mbps = float(max_b_mbps_setting if max_b_mbps_setting is not None else float('inf'))
    
    current_b_kbps = bitrate # This is in Kbps from result
    current_b_mbps = current_b_kbps / 1000.0

    normalized_bitrate_factor = 0.0
    normalized_bitrate = 0.0
    if current_b_mbps >= min_b_mbps:
        if max_b_mbps != float('inf') and max_b_mbps > min_b_mbps:
            normalized_bitrate_factor = (current_b_mbps - min_b_mbps) / (max_b_mbps - min_b_mbps)
        else: # max_b_mbps is infinity or not sensible
            reference_cap_b_mbps = 0.0
            parsed_resolution = parsed_info.get('resolution', 'Unknown').lower()
            if '2160p' in parsed_resolution or '4k' in parsed_resolution:
                reference_cap_b_mbps = 25.0
            elif '1080p' in parsed_resolution:
                reference_cap_b_mbps = 12.0
            elif '720p' in parsed_resolution:
                reference_cap_b_mbps = 6.0
            else: # SD or Unknown
                reference_cap_b_mbps = 3.0

            # Ensure reference_cap_b_mbps is greater than min_b_mbps for sensible scaling
            if reference_cap_b_mbps <= min_b_mbps: 
                reference_cap_b_mbps = min_b_mbps * 2.0 if min_b_mbps > 0 else 1.0

            if reference_cap_b_mbps > min_b_mbps: # Avoid division by zero
                normalized_bitrate_factor = (current_b_mbps - min_b_mbps) / (reference_cap_b_mbps - min_b_mbps)
            else: # Fallback
                normalized_bitrate_factor = 0.0
            
        normalized_bitrate_factor = max(0.0, min(normalized_bitrate_factor, 1.0)) # Clamp to 0-1
        normalized_bitrate = (normalized_bitrate_factor ** 1.5) * 20.0
        
    normalized_country = country_score  # Already in +/-10 range

    # Calculate language score based on preferred language
    language_score = 0
    language_reason = "No language preference or no translated title available"
    if translated_title:
        parsed_title_lang = parsed_info.get('title', '') # Use 'title' from parsed_info for language matching
        normalized_parsed_title_lang = normalize_title(parsed_title_lang).lower()
        normalized_translated_q_title = normalize_title(translated_title).lower()
        title_lang_sim = fuzz.ratio(normalized_parsed_title_lang, normalized_translated_q_title) / 100.0

        similarity_threshold_lang = 0.90 
        if title_lang_sim >= similarity_threshold_lang:
            language_score = 100 
            language_reason = f"High similarity to translated title ({title_lang_sim:.2f} >= {similarity_threshold_lang})"
        else:
            language_score = -25 
            language_reason = f"Low similarity to translated title ({title_lang_sim:.2f} < {similarity_threshold_lang})"

    # --- Language Code Ranking Logic ---
    # Check if this result has language codes and adjust score accordingly
    has_language_codes = result.get('has_language_codes', False)
    detected_language_codes = result.get('detected_language_codes', [])
    
    # If the original title/aliases don't have language codes, prefer items without language codes
    if not has_language_codes:
        # Original doesn't have language codes - prefer items without language codes
        language_score += 50  # Bonus for items without language codes
        language_reason += f" + Bonus for no language codes (original has none)"
    else:
        # Original has language codes - this was already handled in filtering
        # But we can still give a small bonus for having the expected language codes
        if detected_language_codes:
            language_score += 25  # Small bonus for having language codes when expected
            language_reason += f" + Bonus for having language codes: {detected_language_codes}"
    # --- End Language Code Ranking Logic ---

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
             if season_pack_type == 'Complete':
                  # Assume complete pack contains the queried season
                  is_queried_season_pack = True
             elif season_pack_type not in ['N/A', 'Unknown']:
                  is_queried_season_pack = str(query_season) in [s for s in season_pack_type.split(',') if s.isdigit()]

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
    filename_lower = filename.lower() if filename else "" # Use empty string if None
    binge_group_lower = binge_group.lower() if binge_group else "" # Use empty string if None
    fields_to_check_pref = [torrent_title_lower, filename_lower, binge_group_lower]

    # Apply preferred_filter_in bonus
    in_score, in_breakdown = check_preferred(version_settings.get('preferred_filter_in', []), fields_to_check_pref, is_bonus=True)
    preferred_filter_score += in_score
    preferred_filter_in_breakdown = in_breakdown

    # Apply preferred_filter_out penalty
    out_score, out_breakdown = check_preferred(version_settings.get('preferred_filter_out', []), fields_to_check_pref, is_bonus=False)
    preferred_filter_score += out_score # Remember out_score is already negative if matched
    preferred_filter_out_breakdown = out_breakdown

    # --- Language Code Penalties ---
    language_code_penalty = 0
    if result.get('language_code_mismatch_penalty'):
        language_code_penalty += result['language_code_mismatch_penalty']
        logging.info(f"Applied language code mismatch penalty: {result['language_code_mismatch_penalty']} for '{torrent_title}'")
    if result.get('language_code_missing_penalty'):
        language_code_penalty += result['language_code_missing_penalty']
        logging.info(f"Applied language code missing penalty: {result['language_code_missing_penalty']} for '{torrent_title}'")
    if result.get('language_code_unexpected_penalty'):
        language_code_penalty += result['language_code_unexpected_penalty']
        logging.info(f"Applied language code unexpected penalty: {result['language_code_unexpected_penalty']} for '{torrent_title}'")
    # --- End Language Code Penalties ---

    # Combine scores
    total_score = (
        weighted_similarity +
        weighted_resolution +
        weighted_hdr +
        weighted_size +
        weighted_bitrate +
        weighted_country +
        weighted_language +
        weighted_year_match +
        (season_match_score * 5) +
        (episode_match_score * 5) +
        multi_pack_score +
        single_episode_score +
        preferred_filter_score +
        language_code_penalty
    )

    # Add points based on num_items
    from utilities.settings import get_setting
    if get_setting('Debug', 'emphasize_number_of_items_over_quality', False):
        total_score += num_items * 1

    # Content type matching score
    content_type_score = 0
    if content_type.lower() == 'movie' and not result.get('is_anime', False):
        if re.search(r'(s\d{2}|e\d{2}|season|episode)', torrent_title, re.IGNORECASE):
            content_type_score = -500
            #logging.debug(f"Applied penalty for movie with season/episode in title")
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

    # --- START DEBUG LOGGING FOR SPECIFIC TITLE ---
    target_debug_title = "The.Handmaids.Tale.S03.SweSub.1080p.x264-Justiso"
    current_original_title = result.get('original_title', '')

    if current_original_title == target_debug_title:
        logging.info(f"--- Detailed Score Debug for: {target_debug_title} ---")
        logging.info(f"  Query: '{query}', Year: {query_year}, S: {query_season}, E: {query_episode}, Multi: {multi}, Content: {content_type}")
        logging.info(f"  Parsed Info: {parsed_info}")
        logging.info(f"  Version Settings: {version_settings}")
        logging.info(f"  Preferred Lang: {preferred_language}, Translated Title: {translated_title}")
        
        logging.info(f"  --- Raw Similarity & Features ---")
        logging.info(f"    Title Similarity (raw ratio): {title_similarity:.4f} (Normalized Query: '{normalized_query}', Normalized Extracted: '{normalized_extracted_title}')")
        logging.info(f"    Resolution Score (base): {resolution_score} (Parsed Res: {parsed_info.get('resolution', 'Unknown')})")
        logging.info(f"    HDR Score (base): {hdr_score} (Parsed HDR: {parsed_info.get('is_hdr', False)}, HDR Enabled: {version_settings.get('enable_hdr', True)})")
        logging.info(f"    Year Match (base): {year_match} (Query Year: {query_year}, Torrent Year: {torrent_year}, Reason: {year_reason})")
        logging.info(f"    Country Score (base): {country_score} (Media Country: {media_country}, Result Country: {result_country}, Reason: {country_reason})")
        logging.info(f"    Language Score (base): {language_score} (Reason: {language_reason})")

        logging.info(f"  --- Size Calculation ---")
        logging.info(f"    Comparison Size (current_s): {current_s:.4f} GB")
        logging.info(f"    Min Size (min_s): {min_s:.4f} GB, Max Size (max_s): {max_s if max_s != float('inf') else 'inf'} GB")
        logging.info(f"    Normalized Size Factor: {normalized_size_factor:.4f}")
        logging.info(f"    Normalized Size Score (pre-weight): {normalized_size:.4f}")

        logging.info(f"  --- Bitrate Calculation ---")
        logging.info(f"    Bitrate (current_b_kbps): {current_b_kbps:.2f} Kbps ({current_b_mbps:.2f} Mbps)")
        logging.info(f"    Min Bitrate (min_b_mbps): {min_b_mbps:.2f} Mbps, Max Bitrate (max_b_mbps): {max_b_mbps if max_b_mbps != float('inf') else 'inf'} Mbps")
        logging.info(f"    Normalized Bitrate Factor: {normalized_bitrate_factor:.4f}")
        logging.info(f"    Normalized Bitrate Score (pre-weight): {normalized_bitrate:.4f}")

        logging.info(f"  --- Episode Specific Scores (Base) ---")
        logging.info(f"    Season Match Score (base): {season_match_score}")
        logging.info(f"    Episode Match Score (base): {episode_match_score}")
        logging.info(f"    Num Items (for pack calc): {num_items}, Is Multi Pack (for scoring): {is_multi_pack}")
        logging.info(f"    Multi Pack Score (base): {multi_pack_score}")
        logging.info(f"    Single Episode Score (base): {single_episode_score}")

        logging.info(f"  --- Filter & Content Scores ---")
        logging.info(f"    Preferred Filter Score: {preferred_filter_score} (In: {preferred_filter_in_breakdown}, Out: {preferred_filter_out_breakdown})")
        logging.info(f"    Content Type Score: {content_type_score}")

        logging.info(f"  --- Weighted Scores ---")
        logging.info(f"    Weighted Similarity: {weighted_similarity:.4f} (Weight: {similarity_weight})")
        logging.info(f"    Weighted Resolution: {weighted_resolution:.4f} (Weight: {resolution_weight})")
        logging.info(f"    Weighted HDR: {weighted_hdr:.4f} (Weight: {hdr_weight})")
        logging.info(f"    Weighted Size: {weighted_size:.4f} (Weight: {size_weight})")
        logging.info(f"    Weighted Bitrate: {weighted_bitrate:.4f} (Weight: {bitrate_weight})")
        logging.info(f"    Weighted Country: {weighted_country:.4f} (Weight: {country_weight})")
        logging.info(f"    Weighted Language: {weighted_language:.4f} (Weight: {language_weight})")
        logging.info(f"    Weighted Year Match: {weighted_year_match:.4f} (Weight: {year_match_weight})")
        logging.info(f"    Weighted Season Match: {season_match_score * 5:.4f}")
        logging.info(f"    Weighted Episode Match: {episode_match_score * 5:.4f}")
        
        logging.info(f"  --- Final Total Score Components Summed ---")
        logging.info(f"    Sum = {weighted_similarity:.2f} (sim) + {weighted_resolution:.2f} (res) + {weighted_hdr:.2f} (hdr) + " +
                     f"{weighted_size:.2f} (size) + {weighted_bitrate:.2f} (bit) + {weighted_country:.2f} (country) + " +
                     f"{weighted_language:.2f} (lang) + {weighted_year_match:.2f} (year) + " +
                     f"{season_match_score * 5:.2f} (season) + {episode_match_score * 5:.2f} (ep) + " +
                     f"{multi_pack_score:.2f} (pack_bonus) + {single_episode_score:.2f} (single_ep_penalty) + " +
                     f"{preferred_filter_score:.2f} (pref_filter) + {content_type_score:.2f} (content_type) + " +
                     f"{language_code_penalty:.2f} (lang_code_penalty)")
        logging.info(f"  Calculated Total Score (before rounding for breakdown): {total_score}")
        logging.info(f"--- End Detailed Score Debug for: {target_debug_title} ---")
    # --- END DEBUG LOGGING FOR SPECIFIC TITLE ---

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
        'preferred_filter_in_breakdown': preferred_filter_in_breakdown, # Contains original patterns that matched
        'preferred_filter_out_breakdown': preferred_filter_out_breakdown, # Contains original patterns that matched
        'content_type_score': content_type_score,
        'language_code_penalty': language_code_penalty, # Add language code penalties
        'total_score': round(total_score, 2)
    }
    # Add multi-pack information to the score breakdown
    score_breakdown['is_multi_pack'] = is_multi_pack if content_type.lower() == 'episode' else False
    score_breakdown['num_items'] = num_items if content_type.lower() == 'episode' else 1
    logging.debug(f"[RRK] Final num_items for score_breakdown: {score_breakdown['num_items']} for '{torrent_title}'")
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
    # logging.debug(f"├─ Num Items Covered (for ranking): {num_items}") # Add this to logging if needed
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
    # If you want to prioritize by num_items, it would be the first element:
    # return (-num_items, -total_score, -year_match, -season_match_score, -episode_match_score)
    return (-total_score, -year_match, -season_match_score, -episode_match_score, result.get('original_title', ''))