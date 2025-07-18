import logging
import re
from typing import List, Dict, Any, Tuple, Optional
from fuzzywuzzy import fuzz
from PTT import parse_title
from utilities.settings import get_setting
from scraper.functions.similarity_checks import improved_title_similarity, normalize_title
from scraper.functions.file_processing import compare_resolutions, parse_size, calculate_bitrate
from scraper.functions.other_functions import smart_search
from scraper.functions.adult_terms import adult_terms
from scraper.functions.common import *
# --- Import DirectAPI if type hinting is desired, ensure it's available in the execution path ---
# from cli_battery.app.direct_api import DirectAPI # Or adjust path as needed

def filter_results(
    results: List[Dict[str, Any]], tmdb_id: str, title: str, year: int, content_type: str,
    season: int, episode: int, multi: bool, version_settings: Dict[str, Any],
    runtime: int, episode_count: int, season_episode_counts: Dict[int, int],
    genres: List[str], matching_aliases: List[str] = None,
    imdb_id: Optional[str] = None, 
    direct_api: Optional[Any] = None, # Use 'Any' or the specific DirectAPI type
    preferred_language: str = None,
    translated_title: str = None,
    target_air_date: Optional[str] = None,
    check_pack_wantedness: bool = False,
    current_scrape_target_version: Optional[str] = None,
    original_episode: Optional[int] = None  # Add original episode parameter
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:

    # --- START Logging for season_episode_counts ---
    #logging.debug(f"filter_results called for '{title}' S{season}E{episode if episode else ''}. Received season_episode_counts: {season_episode_counts}")
    # --- END Logging ---

    filtered_results = []
    pre_size_filtered_results = []  # Track results before size filtering
    resolution_wanted = version_settings.get('resolution_wanted', '<=')
    max_resolution = version_settings.get('max_resolution', '2160p')
    min_size_gb = float(version_settings.get('min_size_gb', 0.01))
    max_size_gb = float(version_settings.get('max_size_gb', float('inf')) or float('inf'))
    filter_in = version_settings.get('filter_in', [])
    filter_out = version_settings.get('filter_out', [])
    enable_hdr = version_settings.get('enable_hdr', False)
    disable_adult = get_setting('Scraping', 'disable_adult', False)
    
    #logging.debug(f"Starting filter_results with {len(results)} results")
    #logging.debug(f"Version settings: resolution={max_resolution}({resolution_wanted}), size={min_size_gb}-{max_size_gb}GB, HDR={enable_hdr}")
    #logging.debug(f"Filter patterns - in: {filter_in}, out: {filter_out}")
    
    # Pre-compile patterns
    filter_in_patterns = filter_in if filter_in else []
    filter_out_patterns = filter_out if filter_out else []
    adult_pattern = re.compile('|'.join(adult_terms), re.IGNORECASE) if disable_adult else None
    
    # Determine content type specific settings
    is_movie = content_type.lower() == 'movie'
    is_episode = content_type.lower() == 'episode'
    is_anime = genres and any('anime' in g.lower() for g in genres)
    is_ufc = False
    
    # Pre-normalize query title and aliases
    normalized_query_title = normalize_title(title).lower()
    normalized_aliases = [normalize_title(alias).lower() for alias in (matching_aliases or [])]
    normalized_translated_title = normalize_title(translated_title).lower() if translated_title else None
    
    # Determine base similarity threshold
    # Override anime similarity threshold to be more restrictive to prevent false matches
    original_anime_setting = version_settings.get('similarity_threshold_anime', 0.60)
    anime_threshold = max(0.60, float(original_anime_setting))
    base_similarity_threshold = anime_threshold if is_anime else float(version_settings.get('similarity_threshold', 0.8))
    
    # Debug logging for threshold issues
    if is_anime:
        logging.info(f"DEBUG THRESHOLD: Original anime setting: {original_anime_setting}, enforced minimum: {anime_threshold}, is_anime: {is_anime}")
    
    # Adjust threshold for shorter titles if not anime/UFC (which have their own specific low threshold)
    # We'll check is_ufc later in the loop for each result, but for threshold setting,
    # we only care if the *overall query* might be for anime (which has a low threshold).
    # If it's not anime, apply dynamic threshold for short titles.
    similarity_threshold = base_similarity_threshold
    query_title_len = len(normalized_query_title)
    if not is_anime: # Only apply dynamic scaling if not anime (UFC check is per-result)
        if query_title_len < 5:
            similarity_threshold = 1.0
        elif query_title_len < 6:
            similarity_threshold = 0.95
        elif query_title_len < 8:
            similarity_threshold = 0.90
        elif query_title_len < 10:
            similarity_threshold = 0.85
        # If query_title_len >= 10, it uses the base_similarity_threshold (e.g., 0.8)
        # or the anime threshold if is_anime was true.
    logging.info(f"DEBUG: Title length: {query_title_len}, Similarity threshold: {similarity_threshold}")

    #logging.debug(f"Content type: {'movie' if is_movie else 'episode'}, Anime: {is_anime}, Title similarity threshold: {similarity_threshold}")
    
    # Cache season episode counts for multi-episode content
    total_episodes = sum(season_episode_counts.values()) if season_episode_counts and is_episode else 0
    # --- START Logging for total_episodes ---
    #logging.debug(f"Calculated total_episodes for '{title}': {total_episodes} (based on is_episode: {is_episode}, initial season_episode_counts: {season_episode_counts})")
    # --- END Logging ---
    
    # --- Cache for API fallback results within this filter_results call ---
    _fetched_detailed_seasons_data_cache = None
    
    for result in results:
        try:
            result['filter_reason'] = "Passed all filters"
            original_title = result.get('original_title', result.get('title', ''))
            parsed_info = result.get('parsed_info', {})
            additional_metadata = result.get('additional_metadata', {}) # Get additional metadata
            
            # --- Get scraper_type from the result ---
            result_scraper_type = result.get('scraper_type', 'Unknown') # Default to Unknown
            result_scraper_instance = result.get('scraper_instance', 'Unknown')
            # logging.debug(f"Processing result from Scraper Type: {result_scraper_type}, Instance: {result_scraper_instance}")
            
            # Extract potential fields from additional_metadata
            filename = additional_metadata.get('filename')
            binge_group = additional_metadata.get('bingeGroup')
            
            #logging.debug(f"Processing result: {original_title}")
            
            # Quick UFC check
            if "UFC" in original_title.upper():
                is_ufc = True
                similarity_threshold = 0.35
                #logging.debug("UFC content detected, lowering similarity threshold")
            
            # Get parsed info from result (should be already parsed by PTT)
            if not parsed_info:
                result['filter_reason'] = "Missing parsed info"
                logging.info(f"Rejected: Missing parsed info for '{original_title}' (Size: {result['size']:.2f}GB)")
                continue
            
            # Check if it's marked as trash by PTT and filter_trash_releases is enabled
            filter_trash_releases = get_setting('Scraping', 'filter_trash_releases', True)
            if filter_trash_releases and parsed_info.get('trash', False):
                result['filter_reason'] = "Marked as trash by parser"
                logging.info(f"Rejected: Marked as trash by parser for '{original_title}' (Size: {result['size']:.2f}GB)")
                continue
            
            # Store original title in parsed_info
            parsed_info['original_title'] = original_title
            
            # If season_episode_info is not in parsed_info, detect it
            if 'season_episode_info' not in parsed_info:
                from scraper.functions.common import detect_season_episode_info
                parsed_info['season_episode_info'] = detect_season_episode_info(original_title)
                # logging.debug(f"Detected season_episode_info: {parsed_info['season_episode_info']}")
                
                # Special handling for documentary tags that might be misinterpreted as episode titles
                if 'episode_title' in parsed_info and parsed_info.get('episode_title', '').upper() == 'DOC':
                    # This is likely a documentary tag, not an episode title
                    parsed_info['documentary'] = True
                    del parsed_info['episode_title']
                    # Re-detect season/episode info after fixing the parsed_info
                    parsed_info['season_episode_info'] = detect_season_episode_info(parsed_info)
                    # logging.debug(f"Corrected season_episode_info after DOC handling: {parsed_info['season_episode_info']}")
            
            result['parsed_info'] = parsed_info

            
            # Title similarity check
            # Always use the original, full torrent title for comparison.
            # This is the most reliable source of all possible names (e.g., EN, JP, etc.)
            # especially when PTT parsing might be ambiguous.
            # `fuzz.token_set_ratio` is designed to find matching subsets of words.
            comparison_title = original_title
            normalized_result_title = normalize_title(comparison_title).lower()
            
            # If this is a documentary, add it back to the result title for comparison
            if parsed_info.get('documentary', False):
                normalized_result_title = f"{normalized_result_title} documentary"
            
            # Calculate similarities using a blended approach for better accuracy.
            # We combine a lenient check on the full title with a stricter check on the parsed title.
            parsed_title_str = parsed_info.get('title', '')
            normalized_parsed_title = normalize_title(parsed_title_str).lower() if parsed_title_str else None

            # --- Main Title Similarity ---
            main_sim_set = fuzz.token_set_ratio(normalized_result_title, normalized_query_title) / 100.0
            if normalized_parsed_title:
                main_sim_sort = fuzz.token_sort_ratio(normalized_parsed_title, normalized_query_title) / 100.0
                main_title_sim = (main_sim_set + main_sim_sort) / 2.0
            else:
                main_title_sim = main_sim_set

            # --- Alias Similarities ---
            alias_similarities = []
            if normalized_aliases:
                for alias in normalized_aliases:
                    alias_sim_set = fuzz.token_set_ratio(normalized_result_title, alias) / 100.0
                    if normalized_parsed_title:
                        alias_sim_sort = fuzz.token_sort_ratio(normalized_parsed_title, alias) / 100.0
                        alias_similarities.append((alias_sim_set + alias_sim_sort) / 2.0)
                    else:
                        alias_similarities.append(alias_sim_set)
            best_alias_sim = max(alias_similarities) if alias_similarities else 0.0

            # --- Translated Title Similarity ---
            translated_title_sim = 0.0
            if normalized_translated_title:
                trans_sim_set = fuzz.token_set_ratio(normalized_result_title, normalized_translated_title) / 100.0
                if normalized_parsed_title:
                    trans_sim_sort = fuzz.token_sort_ratio(normalized_parsed_title, normalized_translated_title) / 100.0
                    translated_title_sim = (trans_sim_set + trans_sim_sort) / 2.0
                else:
                    translated_title_sim = trans_sim_set

            # Compute initial best similarity score (without API aliases)
            best_sim = max(main_title_sim, best_alias_sim, translated_title_sim)

            # --- Fetch additional aliases via DirectAPI ---
            item_aliases = {}
            try:
                if direct_api:
                    if content_type.lower() == 'movie':
                        item_aliases, _ = direct_api.get_movie_aliases(imdb_id)
                    else:
                        item_aliases, _ = direct_api.get_show_aliases(imdb_id)
            except Exception as alias_err:
                logging.warning(f"Failed to fetch aliases for {imdb_id}: {alias_err}")
                item_aliases = {}

            # Ensure item_aliases is a dictionary even if the API returned None or an unexpected value
            if not isinstance(item_aliases, dict):
                item_aliases = {}

            # -------------------------------------------------------------
            # Include original_title from metadata in alias pool
            # -------------------------------------------------------------
            try:
                if imdb_id and direct_api:
                    if content_type.lower() == 'movie':
                        meta_data, _ = direct_api.get_movie_metadata(imdb_id)
                    else:
                        meta_data, _ = direct_api.get_show_metadata(imdb_id)

                    if meta_data and isinstance(meta_data, dict):
                        orig_title_val = meta_data.get('original_title') or meta_data.get('originalTitle')
                        if orig_title_val:
                            if isinstance(orig_title_val, list):
                                orig_title_list = [str(t) for t in orig_title_val if t]
                            else:
                                orig_title_list = [str(orig_title_val)]

                            existing_orig_list = item_aliases.get('original_title', [])
                            # Merge and ensure uniqueness later in dedup step
                            existing_orig_list.extend(orig_title_list)
                            item_aliases['original_title'] = existing_orig_list
            except Exception as meta_err:
                logging.warning(f"Failed to fetch original_title for {imdb_id}: {meta_err}")

            # Deduplicate and log aliases
            item_aliases = {k: list(set(v)) for k, v in item_aliases.items()}
            
            # --- DEBUG: Log aliases for troubleshooting ---
            if item_aliases:
                logging.info(f"DEBUG: API aliases for '{title}' (IMDb: {imdb_id}): {item_aliases}")

            # -------------------------------------------------------------
            # Re-evaluate alias similarities with the newly fetched aliases
            # -------------------------------------------------------------
            # Build a combined list of aliases (those provided as function
            # arguments plus those fetched from the API) so that every
            # available synonym can help a release pass the similarity check.
            # NOTE: we keep the original `alias_similarities` list because it
            # already contains scores for `matching_aliases`. We now extend it
            # with scores calculated for API aliases and then recompute
            # best_alias_sim / best_sim prior to the threshold comparison.

            item_alias_similarities: list = []
            alias_debug_info = []  # For debugging
            for alias_list in item_aliases.values():
                for alias in alias_list:
                    normalized_api_alias = normalize_title(alias).lower()
                    alias_sim_set = fuzz.token_set_ratio(normalized_result_title, normalized_api_alias) / 100.0
                    if normalized_parsed_title:
                        alias_sim_sort = fuzz.token_sort_ratio(normalized_parsed_title, normalized_api_alias) / 100.0
                        final_alias_sim = (alias_sim_set + alias_sim_sort) / 2.0
                        item_alias_similarities.append(final_alias_sim)
                    else:
                        final_alias_sim = alias_sim_set
                        item_alias_similarities.append(final_alias_sim)
                    
                    # Store debug info for troublesome titles
                    if "araiguma" in original_title.lower() or "calcal" in original_title.lower():
                        alias_debug_info.append({
                            'alias': alias,
                            'normalized_alias': normalized_api_alias,
                            'similarity': final_alias_sim
                        })

            # Combine and (re)compute best alias / best overall similarity
            if item_alias_similarities:
                alias_similarities.extend(item_alias_similarities)
                best_alias_sim = max(alias_similarities)
                best_sim = max(main_title_sim, best_alias_sim, translated_title_sim)

            # -------------------------------------------------------------
            # Check if the best similarity meets the threshold (now updated)
            # -------------------------------------------------------------
            
            # --- ANIME-SPECIFIC SANITY CHECK ---
            # For anime, add additional validation to prevent false matches from fuzzy token overlap
            if is_anime and best_sim >= similarity_threshold:
                logging.info(f"DEBUG SANITY: Running anime sanity check for '{original_title}' (best_sim={best_sim:.3f}, threshold={similarity_threshold:.3f})")
                
                # Check if we have substantial character overlap, not just token fragments
                query_chars = set(normalized_query_title.replace('.', ''))
                result_chars = set(normalized_result_title.replace('.', ''))
                
                # Calculate character overlap ratio
                common_chars = query_chars.intersection(result_chars)
                char_overlap_ratio = len(common_chars) / len(query_chars) if query_chars else 0
                
                # Also check for meaningful word overlap (not just fragments)
                query_words = [w for w in normalized_query_title.split('.') if len(w) > 2]  # Words longer than 2 chars
                result_words = [w for w in normalized_result_title.split('.') if len(w) > 2]
                
                meaningful_word_matches = 0
                for query_word in query_words:
                    for result_word in result_words:
                        # Check for exact word match or strong substring match (90%+ of the word)
                        if (query_word == result_word or 
                            (len(query_word) > 3 and query_word in result_word and len(query_word) / len(result_word) > 0.9) or
                            (len(result_word) > 3 and result_word in query_word and len(result_word) / len(query_word) > 0.9)):
                            meaningful_word_matches += 1
                            break
                
                word_match_ratio = meaningful_word_matches / len(query_words) if query_words else 0
                
                logging.info(f"DEBUG SANITY: char_overlap={char_overlap_ratio:.3f}, word_match={word_match_ratio:.3f} for '{original_title}'")
                
                # Require either strong character overlap OR meaningful word matches for anime
                if char_overlap_ratio < 0.4 and word_match_ratio < 0.5:
                    result['filter_reason'] = f"Anime title failed sanity check (char_overlap={char_overlap_ratio:.2f}, word_match={word_match_ratio:.2f}, similarity={best_sim:.2f})"
                    logging.info(f"Rejected: Anime sanity check failed for '{original_title}' - insufficient substantial overlap despite fuzzy similarity {best_sim:.2f} (Size: {result['size']:.2f}GB)")
                    continue
                else:
                    logging.info(f"DEBUG SANITY: Sanity check passed for '{original_title}'")
            elif is_anime:
                logging.info(f"DEBUG SANITY: Skipping sanity check for '{original_title}' (best_sim={best_sim:.3f} < threshold={similarity_threshold:.3f})")
            
            # --- DEBUG: Log detailed similarity scores for troublesome titles ---
            if "araiguma" in original_title.lower() or "calcal" in original_title.lower():
                logging.info(f"DEBUG SIMILARITY: Analyzing '{original_title}'")
                logging.info(f"  - Normalized result title: '{normalized_result_title}'")
                logging.info(f"  - Normalized query title: '{normalized_query_title}'")
                logging.info(f"  - Main title similarity: {main_title_sim:.3f}")
                logging.info(f"  - Best alias similarity: {best_alias_sim:.3f}")
                logging.info(f"  - Translated title similarity: {translated_title_sim:.3f}")
                logging.info(f"  - Best overall similarity: {best_sim:.3f}")
                logging.info(f"  - Similarity threshold: {similarity_threshold:.3f}")
                if item_alias_similarities:
                    logging.info(f"  - API alias similarities: {[f'{s:.3f}' for s in item_alias_similarities]}")
                if alias_debug_info:
                    for debug_info in alias_debug_info:
                        if debug_info['similarity'] > 0.1:  # Only log aliases with some similarity
                            logging.info(f"    - Alias '{debug_info['alias']}' (normalized: '{debug_info['normalized_alias']}') = {debug_info['similarity']:.3f}")
            
            if best_sim < similarity_threshold:
                # Log the failure reason including all comparison scores
                result['filter_reason'] = f"Title similarity too low (best={best_sim:.2f} < {similarity_threshold})"
                logging.info(f"Rejected: Title similarity too low (best={best_sim:.2f} < {similarity_threshold}) for '{original_title}' (Size: {result['size']:.2f}GB)")
                # logging.debug(f"  - Main title ({main_title_sim:.2f}): '{normalized_result_title}' vs '{normalized_query_title}'")
                # if normalized_aliases:
                #     logging.debug(f"  - Best alias ({best_alias_sim:.2f}): '{normalized_result_title}' vs '{normalized_aliases[alias_similarities.index(best_alias_sim)]}'")
                # if normalized_translated_title:
                #     logging.debug(f"  - Translated title ({translated_title_sim:.2f}): '{normalized_result_title}' vs '{normalized_translated_title}'")
                continue
            # else:
                # Log which title matched - REMOVED FOR SIMPLICITY
                # if main_title_sim >= similarity_threshold:
                #     logging.debug(f"✓ Passed title similarity via main title ({main_title_sim:.2f})")
                # elif best_alias_sim >= similarity_threshold:
                #     logging.debug(f"✓ Passed title similarity via alias ({best_alias_sim:.2f})")
                # elif translated_title_sim >= similarity_threshold:
                #     logging.debug(f"✓ Passed title similarity via translated title ({translated_title_sim:.2f})")
                    
            #logging.debug("✓ Passed title similarity check")
            
            # Resolution check
            detected_resolution = parsed_info.get('resolution', 'Unknown')
            if not resolution_filter(detected_resolution, max_resolution, resolution_wanted):
                result['filter_reason'] = f"Resolution mismatch (max: {max_resolution}, wanted: {resolution_wanted})"
                logging.info(f"Rejected: Resolution '{detected_resolution}' doesn't match criteria '{resolution_wanted} {max_resolution}' for '{original_title}' (Size: {result['size']:.2f}GB)")
                continue
            #logging.debug("✓ Passed resolution check")
            
            # HDR check
            if not enable_hdr and parsed_info.get('is_hdr', False):
                result['filter_reason'] = "HDR content when HDR is disabled"
                logging.info(f"Rejected: HDR content not allowed for '{original_title}' (Size: {result['size']:.2f}GB)")
                continue
            #logging.debug("✓ Passed HDR check")
            
            # Content type specific checks
            if is_movie and not is_ufc:
                parsed_year = parsed_info.get('year')
                if parsed_year:
                    if isinstance(parsed_year, list):
                        if not any(abs(int(py) - year) <= 1 for py in parsed_year):
                            result['filter_reason'] = f"Year mismatch: {parsed_year} (expected: {year})"
                            logging.info(f"Rejected: Movie year list {parsed_year} doesn't match {year} for '{original_title}' (Size: {result['size']:.2f}GB)")
                            continue
                    elif abs(int(parsed_year) - year) > 1:
                        result['filter_reason'] = f"Year mismatch: {parsed_year} (expected: {year})"
                        logging.info(f"Rejected: Movie year {parsed_year} doesn't match {year} for '{original_title}' (Size: {result['size']:.2f}GB)")
                        continue
                #logging.debug("✓ Passed year check")

                # --- Log season_episode_info before the check ---
                season_episode_info = parsed_info.get('season_episode_info', {})
                #logging.debug(f"Movie Check - season_episode_info for '{original_title}': {season_episode_info}")
                # --- End Log ---

                # --- New check for movies: Reject if season/episode info is detected ---
                if season_episode_info:
                    has_seasons_list = bool(season_episode_info.get('seasons'))
                    has_episodes_list = bool(season_episode_info.get('episodes'))
                    season_pack_type = season_episode_info.get('season_pack')
                    
                    # Consider it TV content if seasons/episodes lists are populated,
                    # or if season_pack indicates a pack (not 'N/A' or 'Unknown')
                    is_tv_content_indicator = (
                        has_seasons_list or
                        has_episodes_list or
                        (season_pack_type and season_pack_type not in ['N/A', 'Unknown'])
                    )

                    if is_tv_content_indicator:
                        result['filter_reason'] = "Detected season/episode/pack info for a movie request"
                        logging.info(f"Rejected: Detected TV content indicator for movie request: '{original_title}' (Info: {season_episode_info}) (Size: {result['size']:.2f}GB)")
                        continue
                # --- End new check ---

            elif is_episode:
                # Only apply the special Formula 1 handling to real Formula 1 event releases.
                # The Netflix series "Formula 1: Drive to Survive" should be treated as
                # a regular TV show and go through the normal season-matching logic.
                # Therefore, we enable the Formula 1 override only when the title contains
                # "Formula 1" *and* does NOT contain "Drive to Survive" (case-insensitive).
                title_lower_for_f1_check = title.lower()
                is_formula_1 = ("formula 1" in title_lower_for_f1_check) and ("drive to survive" not in title_lower_for_f1_check)

                if not is_formula_1: # Only perform year check if not Formula 1
                    parsed_year = parsed_info.get('year')
                    if parsed_year:
                        if isinstance(parsed_year, list):
                            # Ensure all elements in parsed_year are convertible to int before comparison
                            try:
                                if not any(abs(int(py) - year) <= 1 for py in parsed_year if str(py).isdigit()):
                                    result['filter_reason'] = f"Year mismatch: {parsed_year} (expected: {year})"
                                    logging.info(f"Rejected: TV year list {parsed_year} doesn't match {year} for '{original_title}' (Size: {result['size']:.2f}GB)")
                                    continue
                            except ValueError:
                                # Handle cases where a year in the list is not a valid integer
                                logging.warning(f"Skipping year check due to invalid year format in list for '{original_title}': {parsed_year}")
                                result['filter_reason'] = f"Invalid year format in list: {parsed_year}"
                                continue

                        elif isinstance(parsed_year, (int, str)) and str(parsed_year).isdigit():
                            if abs(int(parsed_year) - year) > 1:
                                result['filter_reason'] = f"Year mismatch: {parsed_year} (expected: {year})"
                                logging.info(f"Rejected: TV year {parsed_year} doesn't match {year} for '{original_title}' (Size: {result['size']:.2f}GB)")
                                continue
                        else:
                            # Handle cases where parsed_year is not a list or a valid int/str digit
                            logging.warning(f"Skipping year check due to invalid year format for '{original_title}': {parsed_year}")
                            # Optionally, you could reject here if strict year parsing is required
                            # result['filter_reason'] = f"Invalid year format: {parsed_year}"
                            # continue
                else:
                    logging.info(f"Skipping year check for Formula 1 title: '{title}'")
                
                season_episode_info = parsed_info.get('season_episode_info', {})
                #logging.debug(f"Season episode info: {season_episode_info}")
                
                # Check if title contains "complete" - consider it as having all episodes
                if 'complete' in original_title.lower():
                    #logging.debug("Complete series pack detected")
                    season_episode_info['season_pack'] = 'Complete'
                    if season_episode_counts: # Check if the dictionary is not empty
                        season_episode_info['seasons'] = list(season_episode_counts.keys())
                        season_episode_info['episodes'] = list(range(1, max(season_episode_counts.values()) + 1))
                    else:
                        # If season_episode_counts is empty, we can't determine seasons or max episodes
                        season_episode_info['seasons'] = []
                        season_episode_info['episodes'] = []
                        # logging.debug("Complete pack detected but season_episode_counts is empty. Setting seasons/episodes to empty.")
                    result['parsed_info']['season_episode_info'] = season_episode_info
                
                if multi:
                    #logging.debug(f"Multi-episode mode: season={season}, season_pack={season_episode_info.get('season_pack')}, seasons={season_episode_info.get('seasons')}")
                    
                    episodes = season_episode_info.get('episodes', [])
                    if len(episodes) == 1:
                        result['filter_reason'] = "Single episode result when searching for multi"
                        logging.info(f"Rejected: Single episode in multi mode for '{original_title}' (Size: {result['size']:.2f}GB)")
                        continue

                    if episodes and episode is not None and episode not in episodes:
                        result['filter_reason'] = f"Multi-episode pack does not contain requested episode {episode}"
                        logging.info(f"Rejected: Multi-pack missing episode {episode} for '{original_title}' (Size: {result['size']:.2f}GB)")
                        continue

                    season_pack = season_episode_info.get('season_pack', 'Unknown')
                    if season_pack == 'N/A':
                        result['filter_reason'] = "Single episode result when searching for multi"
                        logging.info(f"Rejected: Single episode in multi mode for '{original_title}' (Size: {result['size']:.2f}GB)")
                        continue
                    elif season_pack == 'Complete':
                        #logging.debug("Complete series pack accepted")
                        pass
                    elif season_pack == 'Unknown':
                        if len(episodes) < 2:
                            # For anime, add heuristic detection of season packs that PTT missed
                            is_likely_anime_pack = False
                            
                            logging.info(f"ANIME PACK DEBUG: Processing '{original_title}' - is_anime={is_anime}, genres={genres}")
                            
                            if is_anime:
                                # Check for anime pack indicators in the title
                                anime_pack_indicators = [
                                    'batch', 'complete', 'collection', 'fin', 'finished',
                                    'bdrip', 'bluray', 'bd', 'series', 'season', 'vol',
                                    'dual audio', 'dual-audio', 'multi-sub', 'multi sub'
                                ]
                                
                                title_lower = original_title.lower()
                                has_pack_keywords = any(indicator in title_lower for indicator in anime_pack_indicators)
                                
                                # Debug which indicators matched
                                matched_indicators = [indicator for indicator in anime_pack_indicators if indicator in title_lower]
                                logging.info(f"ANIME PACK DEBUG: title_lower='{title_lower}', matched_indicators={matched_indicators}, has_pack_keywords={has_pack_keywords}")
                                
                                # Check file size - anime season packs are typically larger
                                result_size = parse_size(result.get('size', 0))
                                large_size = result_size > 5.0  # 5GB+ suggests multiple episodes
                                
                                # Check if no explicit episode numbers were found
                                no_explicit_episodes = not bool(re.search(r'\b(?:e|ep|episode)\s*\d+\b', title_lower))
                                
                                is_likely_anime_pack = has_pack_keywords or (large_size and no_explicit_episodes)
                                
                                if is_likely_anime_pack:
                                    logging.info(f"Anime pack heuristic: Treating '{original_title}' as season pack (keywords={has_pack_keywords}, large_size={large_size}GB, no_episodes={no_explicit_episodes})")
                                    # Override the season_episode_info to mark as season pack
                                    season_episode_info['season_pack'] = f'S{season}' if season else 'S1'
                                    season_episode_info['seasons'] = [season] if season else [1]
                                    result['parsed_info']['season_episode_info'] = season_episode_info
                                    # Don't continue - let it pass through as a valid pack
                                else:
                                    logging.info(f"PTT Parse Debug for '{original_title}' (Size: {result_size:.1f}GB):")
                                    logging.info(f"  - season_episode_info: {season_episode_info}")
                                    logging.info(f"  - Anime pack heuristic failed: keywords={has_pack_keywords}, large_size={large_size}, no_episodes={no_explicit_episodes}")
                            
                            if not is_likely_anime_pack:
                                if check_pack_wantedness:
                                    result['filter_reason'] = "Non-multi result when searching for multi (pack wantedness check active)"
                                    logging.info(f"Rejected: Not enough episodes for multi mode for '{original_title}' (is_anime={is_anime}, heuristic_failed={not is_likely_anime_pack}, pack_wantedness_check=True) (Size: {result['size']:.2f}GB)")
                                    continue
                                else:
                                    logging.info(f"Skipping 'Not enough episodes for multi mode' rejection for '{original_title}' as check_pack_wantedness is False. (is_anime={is_anime}, heuristic_failed={not is_likely_anime_pack}) (Size: {result['size']:.2f}GB)")
                                    # If check_pack_wantedness is false, do not 'continue' here.
                                    # Let it proceed to other filters.
                    else:
                        if season not in season_episode_info.get('seasons', []):
                            result['filter_reason'] = f"Season pack not containing the requested season: {season}"
                            logging.info(f"Rejected: Season pack missing season {season} for '{original_title}' (Size: {result['size']:.2f}GB)")
                            continue
                    #logging.debug("✓ Passed multi-episode checks")
                else: # Single episode mode
                    #logging.debug(f"Single episode mode: S{season}E{episode}")
                    
                    result_seasons = season_episode_info.get('seasons', [])
                    result_episodes = season_episode_info.get('episodes', [])
                    
                    # Determine if parsed season info is missing or defaulted (for anime leniency later)
                    parsed_season_is_missing_or_default = not result_seasons or result_seasons == [1]

                    # --- Season Check --- 
                    season_match = False
                    lenient_season_pass = False # Flag if we passed season check due to leniency
                    explicit_season_mismatch = False # Flag if title explicitly mentions a different season

                    if is_formula_1:
                        parsed_torrent_year_val = parsed_info.get('year')
                        actual_torrent_year = None
                        if isinstance(parsed_torrent_year_val, list):
                            if parsed_torrent_year_val and str(parsed_torrent_year_val[0]).isdigit():
                                actual_torrent_year = int(parsed_torrent_year_val[0])
                        elif parsed_torrent_year_val and str(parsed_torrent_year_val).isdigit():
                            actual_torrent_year = int(parsed_torrent_year_val)

                        # For Formula 1, the 'season' parameter to this function is the event year.
                        # We match if the torrent's parsed year equals this event year.
                        # We also expect PTT to parse "Formula.1" as S01 or no season.
                        if actual_torrent_year and actual_torrent_year == season:
                            if not result_seasons or result_seasons == [1]: # Common for F1 torrents named like "Formula.1.2024..."
                                season_match = True
                                logging.info(f"Formula 1: Matched event year {season} with torrent's parsed year {actual_torrent_year}. Parsed seasons from PTT: {result_seasons}. Title: '{original_title}'")
                            else:
                                # This case means year matches, but PTT found an unexpected season number (not S1 or empty)
                                logging.warning(f"Formula 1: Event year {season} matched torrent year {actual_torrent_year}, but PTT parsed seasons {result_seasons} (expected [1] or empty) for '{original_title}'. Treating as season mismatch.")
                                # season_match remains False
                        else:
                            # Torrent's own year does not match the requested event year
                            logging.info(f"Formula 1: Torrent's parsed year {actual_torrent_year} did not match requested event year {season} for '{original_title}'.")
                            # season_match remains False
                    
                    else: # Original logic for non-Formula 1 content
                        if season in result_seasons:
                            # Parsed season explicitly matches the target season
                            season_match = True
                        elif is_anime and parsed_season_is_missing_or_default and season > 1:
                             # Check if title explicitly mentions a different season before applying leniency
                             # Example: Searching S7, title says "S01". We should NOT be lenient here.
                             # Allow leniency only if no other season is clearly stated.
                             if not re.search(rf'[Ss](?!{season:02d})\\d\\d?', original_title):
                                 season_match = True
                                 lenient_season_pass = True
                                 #logging.debug(f"Allowing anime result ({original_title}) parsed as S1/None when target is S{season} (no conflicting season found in title)")
                             else:
                                explicit_season_mismatch = True
                                #logging.debug(f"Anime result ({original_title}) parsed as S1/None has conflicting season info when target is S{season}. Not applying leniency.")
                        elif not result_seasons:
                             # Allow titles with NO season info at all (might be absolute)
                             season_match = True
                             lenient_season_pass = True # Mark as lenient pass
                             #logging.debug(f"Allowing result ({original_title}) with no season info to pass season check")

                    if not season_match:
                        # Reject if we didn't find an explicit match OR a lenient pass
                        # Also reject if leniency was skipped due to explicit conflicting season info
                        reason = f"Season mismatch: expected S{season}, parsed {result_seasons}"
                        if is_formula_1 and actual_torrent_year and actual_torrent_year != season:
                            reason = f"Formula 1 season/event year mismatch: expected event year S{season} (to match torrent's content year), torrent's parsed year was {actual_torrent_year}"
                        elif is_formula_1 and (result_seasons and result_seasons != [1]):
                             reason = f"Formula 1 season parsing mismatch: expected S1 from torrent title, PTT parsed {result_seasons}"


                        if explicit_season_mismatch and not is_formula_1: # Add original_title context for non-F1
                             reason += " (and title mentions conflicting season)"
                        result['filter_reason'] = reason
                        logging.info(f"Rejected: {reason} for '{original_title}' (Size: {result['size']:.2f}GB)")
                        continue
                    #logging.debug(f"✓ Passed season check {('(leniently)' if lenient_season_pass else '')}")
                    # --- End Season Check ---
                    
                    # --- Pack Checks (Reject packs in single mode) ---
                    season_pack = season_episode_info.get('season_pack', 'Unknown')
                    # Check for multi-season packs (e.g., "Complete", "S01,S02")
                    if (season_pack == 'Complete' or (season_pack not in ['N/A', 'Unknown'] and ',' in season_pack)):
                        result['filter_reason'] = "Multi-season pack when searching for single episode"
                        logging.info(f"Rejected: Multi-season pack in single episode mode for '{original_title}' (Size: {result['size']:.2f}GB)")
                        continue

                    # Check for single season packs (parsed season/pack, but no parsed episodes matching target)
                    # This needs to be robust against the loose episode check later
                    is_potential_single_season_pack = season_pack not in ['N/A', 'Unknown'] and not result_episodes
                    
                    # Also check if multiple distinct episodes are detected explicitly
                    if len(result_episodes) > 1:
                        result['filter_reason'] = f"Multiple episodes detected: {result_episodes} when searching for single episode {episode}"
                        logging.info(f"Rejected: Multiple episodes {result_episodes} in single episode mode for '{original_title}' (Size: {result['size']:.2f}GB)")
                        continue
                    # --- End Pack Checks ---

                    # --- Episode Check --- 
                    episode_match = False
                    parsed_date_str = None # Initialize parsed date string
                    #logging.debug(f"Inspecting parsed_info for '{original_title}': {parsed_info}") # Log parsed_info

                    # Attempt to parse date from title
                    # Priority 1: Check for pre-formatted 'date' key from PTT
                    if isinstance(parsed_info.get('date'), str) and re.match(r'^\d{4}-\d{2}-\d{2}$', parsed_info['date']):
                        parsed_date_str = parsed_info['date']
                        #logging.debug(f"Using pre-parsed date '{parsed_date_str}' from parsed_info['date'] for '{original_title}'")
                    # Priority 2: Fallback to constructing from year, month, day keys
                    elif parsed_info.get('year') and parsed_info.get('month') and parsed_info.get('day'):
                        try:
                            parsed_year = int(parsed_info['year'])
                            parsed_month = int(parsed_info['month'])
                            parsed_day = int(parsed_info['day'])
                            parsed_date_str = f"{parsed_year:04d}-{parsed_month:02d}-{parsed_day:02d}"
                            #logging.debug(f"Constructed date '{parsed_date_str}' from year/month/day keys for '{original_title}'")
                        except (ValueError, TypeError) as date_parse_err:
                            logging.warning(f"Could not parse date components from year/month/day keys in '{original_title}': {parsed_info}. Error: {date_parse_err}")
                            parsed_date_str = None # Reset on error
                    # else: parsed_date_str remains None if neither method works

                    if not result_episodes:
                        # Case 1: No episode numbers parsed by PTT. Check date or fallback number.
                        comparison_result = "Skipped" # Default if check doesn't run
                        if target_air_date and parsed_date_str:
                             # Perform the comparison
                             date_match = (target_air_date == parsed_date_str)
                             comparison_result = f"Target='{target_air_date}' == Parsed='{parsed_date_str}' -> {date_match}"
                             if date_match:
                                 episode_match = True
                                 #logging.debug(f"Episode matched via date: {comparison_result} for '{original_title}'")
                        elif not lenient_season_pass and not is_potential_single_season_pack:
                             # Only log comparison if the primary date check didn't pass/run
                             #logging.debug(f"Date check failed or skipped. Target='{target_air_date}', Parsed='{parsed_date_str}'. Falling back to number check.")
                             pass
                             if re.search(rf'\b{episode}\b', original_title):
                                 episode_match = True
                                 #logging.debug(f"Episode number {episode} found directly in title '{original_title}' (non-lenient season match, not a detected pack).")
                        else:
                            # Log why we didn't even attempt the fallback number check
                            #logging.debug(f"Date check failed/skipped (Target='{target_air_date}', Parsed='{parsed_date_str}') and fallback number check skipped (lenient_season={lenient_season_pass}, potential_pack={is_potential_single_season_pack}).")
                            pass
                            
                        # Log the comparison result if the primary date check was attempted
                        if target_air_date and parsed_date_str and not episode_match:
                             #logging.debug(f"Date comparison result: {comparison_result} for '{original_title}'")
                             pass

                    elif episode in result_episodes:
                        # Case-2: parsed episode matches the mapped episode (S/E match).
                        # For anime we previously required an absolute-number match, but this was
                        # causing valid Season/Episode releases such as S03E11 to be rejected.
                        # Accept the direct S/E match here; absolute-number logic below still
                        # provides an additional matching path when torrents use absolute numbers.
                        episode_match = True
                        logging.debug(f"Episode matched via XEM-mapped episode {episode} for '{original_title}'")
                    # --- Anime absolute-number fall-back -----------------------
                    elif is_anime:
                        try:
                            # If the scrape pipeline provided the original absolute episode number
                            # (before XEM season/episode remapping) use it directly.
                            original_abs = result.get('target_abs_episode')
                            if original_abs and (
                                original_abs in result_episodes or
                                re.search(rf'\b{original_abs}\b', original_title)):
                                episode_match = True
                                logging.info(
                                    f"Anime absolute fallback (orig abs): matched absolute "
                                    f"{original_abs} for '{original_title}'")
                            else:
                                # Convert mapped S/E back to the show's absolute number
                                abs_target = 0
                                if season_episode_counts:
                                    for s_num in sorted(k for k in season_episode_counts
                                                        if isinstance(k, int) and k < season):
                                        abs_target += season_episode_counts.get(s_num, 0)
                                abs_target += episode           # <- mapped episode (e.g. 4) gives 16
                                # Accept if torrent explicitly carries that absolute number
                                if (abs_target in result_episodes or
                                    re.search(rf'\b{abs_target}\b', original_title)):
                                    episode_match = True
                                    logging.info(
                                        f"Anime absolute fallback: matched absolute "
                                        f"{abs_target} for '{original_title}'")
                        except Exception as abs_err:
                            logging.warning(f"Absolute-fallback error for "
                                            f"'{original_title}': {abs_err}")
                    # -----------------------------------------------------------------

                    # --- Use original episode for final episode matching if available ---
                    if not episode_match and original_episode is not None and original_episode != episode:
                        logging.debug(f"Trying original episode fallback: original_episode={original_episode}, xem_episode={episode}, result_episodes={result_episodes} for '{original_title}'")
                        # Try matching against the original episode number
                        if original_episode in result_episodes:
                            episode_match = True
                            logging.info(f"Episode matched via original episode number {original_episode} for '{original_title}'")
                        elif re.search(rf'\b{original_episode}\b', original_title):
                            episode_match = True
                            logging.info(f"Episode matched via original episode number {original_episode} found in title for '{original_title}'")
                    # --- End original episode fallback ---

                    if not episode_match:
                        # Before rejecting, check if it was identified as a potential single season pack earlier
                        reason_suffix = f"from '{original_title}'"
                        if is_potential_single_season_pack:
                            result['filter_reason'] = f"Season pack '{season_pack}' detected when searching for single episode {episode} (no matching episode found)"
                            logging.info(f"Rejected: Season pack '{season_pack}' in single episode mode (no matching episode found) {reason_suffix} (Size: {result['size']:.2f}GB)")
                        elif parsed_date_str and target_air_date:
                             # If dates were available but didn't match
                             result['filter_reason'] = f"Date mismatch: needed {target_air_date}, parsed {parsed_date_str}"
                             logging.info(f"Rejected: Date mismatch - needed {target_air_date}, parsed {parsed_date_str} {reason_suffix} (Size: {result['size']:.2f}GB)")
                        else:
                            # General episode mismatch
                            result['filter_reason'] = f"Episode mismatch: needed E{episode}, parsed {result_episodes} (or not found explicitly in title/date)"
                            logging.info(f"Rejected: Episode mismatch - needed E{episode}, parsed {result_episodes} {reason_suffix} (Size: {result['size']:.2f}GB)")
                        continue
                    #logging.debug("✓ Passed episode checks")
                    # --- End Episode Check ---
            
            # --- START Logging before size calculation ---
            #logging.debug(f"Processing for size calc: '{original_title}', is_episode: {is_episode}")
            #logging.debug(f"Full parsed_info: {result.get('parsed_info')}")
            current_season_episode_info = result.get('parsed_info', {}).get('season_episode_info', {})
            #logging.debug(f"season_episode_info for '{original_title}': {current_season_episode_info}")
            current_season_pack_type = current_season_episode_info.get('season_pack', 'Unknown')
            #logging.debug(f"season_pack_type_from_parse for '{original_title}': {current_season_pack_type}")
            current_parsed_ep_list = current_season_episode_info.get('episodes', [])
            #logging.debug(f"parsed_episodes_list_from_parse for '{original_title}': {current_parsed_ep_list}")
            # --- END Logging before size calculation ---
            
            # Size calculation
            total_size_gb = parse_size(result.get('size', 0)) # Raw total size of the torrent
            size_gb_for_filter = total_size_gb # Default: use total size (for movies or single episodes)
            
            num_episodes_in_pack = 0 
            is_identified_as_pack = False 
            
            # --- Check if scraper already provides per-item size ---
            provides_per_item_size = result_scraper_type in ['Torrentio', 'MediaFusion']
            
            if is_episode:
                season_episode_info = result.get('parsed_info', {}).get('season_episode_info', {})
                season_pack_type_from_parse = season_episode_info.get('season_pack', 'Unknown') 
                parsed_episodes_list_from_parse = season_episode_info.get('episodes', [])
                
                is_identified_as_pack = (season_pack_type_from_parse not in ['N/A', 'Unknown']) or \
                                        (len(parsed_episodes_list_from_parse) > 1)

                # --- START: New Pack Wantedness Check ---
                from database.database_reading import get_all_media_items

                if check_pack_wantedness and is_identified_as_pack and imdb_id and direct_api:
                    logging.debug(f"Performing pack wantedness check for '{original_title}' (IMDb: {imdb_id}, Pack Type: {season_pack_type_from_parse}, Target Version: {current_scrape_target_version})")
                    pack_is_fully_wanted = True # Assume true initially

                    # Get items from DB in Wanted or Scraping state for this IMDb ID and Target Version
                    pending_db_items_for_show_raw = get_all_media_items(imdb_id=imdb_id)
                    pending_db_episodes = [
                        item for item in pending_db_items_for_show_raw
                        if item.get('state') in ["Wanted", "Scraping"] and \
                           item.get('type') == 'episode' and \
                           (item.get('version') or "").strip('*') == (current_scrape_target_version or "").strip('*')
                    ]
                    
                    pending_episodes_set = set()
                    for item in pending_db_episodes:
                        s_num = item.get('season_number')
                        e_num = item.get('episode_number')
                        if isinstance(s_num, int) and isinstance(e_num, int):
                            pending_episodes_set.add((s_num, e_num))
                    logging.debug(f"Found {len(pending_episodes_set)} unique S/E pairs in Wanted/Scraping for {imdb_id}: {sorted(list(pending_episodes_set))[:20]}...")

                    # Get detailed show metadata (seasons and episodes)
                    # Use the existing cache pattern for _fetched_detailed_seasons_data_cache
                    show_metadata_for_pack_check = None
                    if _fetched_detailed_seasons_data_cache is not None:
                        show_metadata_for_pack_check = _fetched_detailed_seasons_data_cache
                    else:
                        try:
                            detailed_s_data, _ = direct_api.get_show_seasons(imdb_id=imdb_id)
                            _fetched_detailed_seasons_data_cache = detailed_s_data if detailed_s_data else {} # Cache it
                            show_metadata_for_pack_check = _fetched_detailed_seasons_data_cache
                        except Exception as api_err:
                            logging.warning(f"API error fetching show season data for {imdb_id} for pack wantedness check: {api_err}")
                    
                    if not show_metadata_for_pack_check:
                        logging.warning(f"Could not get show season data for {imdb_id} to check pack wantedness. Skipping this filter for '{original_title}'.")
                    else:
                        expected_episodes_for_pack_scope = set()
                        pack_scope_description = ""

                        if season_pack_type_from_parse == 'Complete':
                            pack_scope_description = "entire series"
                            for s_num, s_data in show_metadata_for_pack_check.items():
                                if isinstance(s_data, dict):
                                    # Use episode_count if available, otherwise count episodes in the dict
                                    ep_count = s_data.get('episode_count')
                                    if ep_count is None: # Fallback to len(episodes dict)
                                        ep_count = len(s_data.get('episodes', {}))
                                    
                                    for e_num in range(1, ep_count + 1):
                                        expected_episodes_for_pack_scope.add((s_num, e_num))
                        elif season_pack_type_from_parse not in ['N/A', 'Unknown']: # Specific season(s) pack
                            pack_scope_description = f"season(s) {season_pack_type_from_parse}"
                            season_numbers_in_pack_str = season_pack_type_from_parse.split(',')
                            parsed_season_numbers_in_pack = []
                            for s_str in season_numbers_in_pack_str:
                                try:
                                    parsed_season_numbers_in_pack.append(int(s_str.strip().lstrip('S').lstrip('s')))
                                except ValueError:
                                    logging.warning(f"Could not parse season number '{s_str}' from pack type '{season_pack_type_from_parse}' for {original_title}")
                            
                            for s_num in parsed_season_numbers_in_pack:
                                s_data = show_metadata_for_pack_check.get(s_num)
                                if isinstance(s_data, dict):
                                    ep_count = s_data.get('episode_count')
                                    if ep_count is None:
                                        ep_count = len(s_data.get('episodes', {}))
                                    for e_num in range(1, ep_count + 1):
                                        expected_episodes_for_pack_scope.add((s_num, e_num))
                                else:
                                    logging.warning(f"No metadata found for S{s_num} in show {imdb_id} for pack wantedness check of '{original_title}'.")
                        else: # Pack identified by multiple PTT parsed episodes, not by a season_pack string
                              # This case means we don't have a clear "pack scope" like "Complete" or "S01".
                              # We only know PTT found multiple episode numbers.
                              # The current logic doesn't have a defined way to determine the *intended* scope of such a pack from metadata.
                              # For now, this specific wantedness check might be skipped for such packs,
                              # or we could be very strict and assume it must match exactly what PTT parsed if those are few.
                              # Given the request, focusing on "Series Pack" and "Season Pack", we might skip this for "loose" multi-episode torrents.
                            logging.debug(f"Pack '{original_title}' identified by PTT episode list, not a defined 'Complete' or 'Season X' pack. Skipping specific wantedness check for now.")
                            # To effectively skip, ensure expected_episodes_for_pack_scope remains empty or pack_is_fully_wanted remains true.
                            # For safety, let's ensure it passes if scope is undetermined here.
                            expected_episodes_for_pack_scope = set() # No specific scope defined to check against.

                        if expected_episodes_for_pack_scope: # Only proceed if we have a defined set of episodes to check for
                            if not expected_episodes_for_pack_scope.issubset(pending_episodes_set):
                                pack_is_fully_wanted = False
                                missing_episodes = sorted(list(expected_episodes_for_pack_scope - pending_episodes_set))
                                logging.info(f"Rejected: Pack '{original_title}' for {pack_scope_description} is not fully wanted/scraping. Missing {len(missing_episodes)} episodes, e.g., {missing_episodes[:5]} (Size: {result['size']:.2f}GB)")
                                result['filter_reason'] = f"Pack for {pack_scope_description} not fully wanted/scraping (missing {len(missing_episodes)} episodes)"
                            else:
                                logging.info(f"Pack '{original_title}' for {pack_scope_description} is fully wanted/scraping. (Size: {result['size']:.2f}GB)")
                        
                    if not pack_is_fully_wanted:
                        continue # Skip to the next result if the pack isn't fully wanted
                # --- END: New Pack Wantedness Check ---

                if is_identified_as_pack:
                    # If scraper provides per-item size, num_episodes_in_pack effectively becomes 1 for size calculation
                    # But we still want to calculate the true num_episodes_in_pack for bitrate
                    
                    # Calculate true number of episodes in the pack for bitrate calculation
                    if season_pack_type_from_parse == 'Complete':
                        num_episodes_in_pack = total_episodes
                        if num_episodes_in_pack == 0 and imdb_id and direct_api:
                            # API fallback logic for 'Complete' packs (as previously implemented)
                            logging.warning(f"'Complete' pack '{original_title}' but initial total_episodes is 0. Attempting API fallback for imdb_id: {imdb_id}.")
                            if _fetched_detailed_seasons_data_cache is None:
                                try:
                                    detailed_s_data, _ = direct_api.get_show_seasons(imdb_id=imdb_id)
                                    _fetched_detailed_seasons_data_cache = detailed_s_data if detailed_s_data else {}
                                except Exception as api_err:
                                    logging.error(f"API fallback direct_api.get_show_seasons failed for {imdb_id} (Complete): {api_err}")
                                    _fetched_detailed_seasons_data_cache = {}
                            if _fetched_detailed_seasons_data_cache:
                                api_total_eps = sum(s_data.get('episode_count', 0) for s_data in _fetched_detailed_seasons_data_cache.values())
                                if api_total_eps > 0:
                                    num_episodes_in_pack = api_total_eps
                                    logging.info(f"API fallback for 'Complete' pack '{original_title}'. Set num_episodes_in_pack to {num_episodes_in_pack}")
                    elif season_pack_type_from_parse not in ['N/A', 'Unknown']:
                        # Logic for specific season packs (S01, S01,S02) to calculate num_episodes_in_pack
                        # (as previously implemented, including API fallback)
                        current_sum = 0
                        try:
                            season_numbers_in_pack = []
                            raw_season_parts = season_pack_type_from_parse.split(',')
                            for part in raw_season_parts:
                                cleaned_part = part.strip().lstrip('S').lstrip('s')
                                if cleaned_part.isdigit():
                                    season_numbers_in_pack.append(int(cleaned_part))
                            if season_numbers_in_pack:
                                for s_num in season_numbers_in_pack:
                                    ep_count_for_season = season_episode_counts.get(s_num, 0) if season_episode_counts else 0
                                    if ep_count_for_season == 0 and imdb_id and direct_api:
                                        # API fallback logic for specific season
                                        logging.warning(f"Season S{s_num} count is 0 for '{original_title}'. Attempting API fallback for imdb_id: {imdb_id}.")
                                        if _fetched_detailed_seasons_data_cache is None:
                                            try:
                                                detailed_s_data, _ = direct_api.get_show_seasons(imdb_id=imdb_id)
                                                _fetched_detailed_seasons_data_cache = detailed_s_data if detailed_s_data else {}
                                            except Exception as api_err:
                                                logging.error(f"API fallback direct_api.get_show_seasons failed for {imdb_id} (S{s_num}): {api_err}")
                                                _fetched_detailed_seasons_data_cache = {}
                                        if s_num in _fetched_detailed_seasons_data_cache:
                                            fetched_s_data = _fetched_detailed_seasons_data_cache[s_num]
                                            ep_count_for_season = fetched_s_data.get('episode_count', 0)
                                            logging.info(f"API fallback for S{s_num} of '{original_title}' got episode_count: {ep_count_for_season}")
                                    current_sum += ep_count_for_season
                                num_episodes_in_pack = current_sum
                        except ValueError: # Fallback for parsing error
                            if len(parsed_episodes_list_from_parse) > 1:
                                num_episodes_in_pack = len(parsed_episodes_list_from_parse)
                    else: # Pack identified by PTT parsed episodes list
                        if len(parsed_episodes_list_from_parse) > 1:
                            num_episodes_in_pack = len(parsed_episodes_list_from_parse)

                    # If num_episodes_in_pack is still 0 after attempts, try PTT's list again
                    if num_episodes_in_pack == 0 and len(parsed_episodes_list_from_parse) > 1:
                         num_episodes_in_pack = len(parsed_episodes_list_from_parse)
                         logging.warning(f"Pack '{original_title}': num_episodes_in_pack was 0, used PTT parsed_episodes_list_from_parse length: {num_episodes_in_pack}")


                    if provides_per_item_size:
                        logging.debug(f"Scraper '{result_scraper_type}' provides per-item size. Using total_size_gb ({total_size_gb:.2f}GB) directly for filtering '{original_title}'.")
                        size_gb_for_filter = total_size_gb # Already the per-item size
                        # num_episodes_in_pack is still needed for accurate bitrate of the single item Torrentio/MediaFusion represents
                        if num_episodes_in_pack == 0: # If it's a pack but we couldn't count episodes
                           if len(parsed_episodes_list_from_parse) > 0 : # PTT might have parsed episode numbers even if it's a single file from Torrentio
                               num_episodes_in_pack = len(parsed_episodes_list_from_parse)
                           else: # Assume it's one episode if Torrentio/Mediafusion and no other info
                               num_episodes_in_pack = 1 
                               logging.debug(f"For {result_scraper_type} result '{original_title}', assuming 1 episode for bitrate as pack count is 0.")
                    elif num_episodes_in_pack > 0:
                        size_gb_for_filter = total_size_gb / num_episodes_in_pack
                    else:
                        # This is the final fallback if not Torrentio/Mediafusion and still no episode count
                        logging.warning(f"Pack '{original_title}' (Scraper: {result_scraper_type}): Could not determine episode count. Using total pack size {total_size_gb:.2f}GB for filtering.")
                        size_gb_for_filter = total_size_gb


            # --- ADDED DEBUG LOGGING ---
            if original_title == "The.Handmaids.Tale.S03.SweSub.1080p.x264-Justiso":
                logging.info(f"[FILTER_DEBUG HANDMAIDS] Pre-final size assignment for '{original_title}':")
                logging.info(f"  total_size_gb: {total_size_gb:.4f}")
                logging.info(f"  num_episodes_in_pack (calc'd in filter_results): {num_episodes_in_pack}")
                logging.info(f"  provides_per_item_size: {provides_per_item_size}")
                logging.info(f"  size_gb_for_filter (to be assigned to result['size']): {size_gb_for_filter:.4f}")
            # --- END ADDED DEBUG LOGGING ---
            result['size'] = size_gb_for_filter 
            result['total_size_gb'] = total_size_gb
            
            # --- Bitrate Calculation Prep ---
            # 'num_episodes_in_pack' at this point has its final value after considering pack type, API fallbacks.
            # 'total_size_gb' is the raw size from the scraper.
            # 'size_gb_for_filter' is the per-item or per-average-item size.

            bitrate = 0
            # runtime is the base runtime for one item (episode or movie) passed into filter_results
            
            actual_ep_count_for_bitrate_calc = 1 # Default for movies or single episodes not identified as packs

            if is_episode:
                if is_identified_as_pack:
                    if provides_per_item_size:
                        # If scraper says total_size_gb is per item, then bitrate is for 1 item's runtime.
                        actual_ep_count_for_bitrate_calc = 1
                        # total_size_gb here is the size of ONE episode as reported by Torrentio/MediaFusion
                    else:
                        # If scraper total_size_gb is for the whole pack, then bitrate is for N items' runtime.
                        # Fallback to 1 if pack count is bad.
                        actual_ep_count_for_bitrate_calc = num_episodes_in_pack if num_episodes_in_pack > 0 else 1
                        if num_episodes_in_pack <= 0: 
                            logging.warning(f"Pack '{original_title}' (not provides_per_item_size): num_episodes_in_pack is {num_episodes_in_pack}. Using 1 for runtime calculation. Bitrate may be inaccurate.")
                # else (single episode not a pack): actual_ep_count_for_bitrate_calc remains 1. This is correct.
            # else (movie): actual_ep_count_for_bitrate_calc remains 1. This is correct.

            effective_runtime_for_bitrate = runtime * actual_ep_count_for_bitrate_calc

            if effective_runtime_for_bitrate > 0 and total_size_gb > 0:
                # The total_size_gb used here should correspond to the number of episodes in actual_ep_count_for_bitrate_calc
                # If provides_per_item_size is True and it's a pack, total_size_gb is already the per-item size.
                # If provides_per_item_size is False and it's a pack, total_size_gb is the full pack size.
                # This logic is correct as calculate_bitrate expects the total size for the given total runtime.
                bitrate = calculate_bitrate(total_size_gb, effective_runtime_for_bitrate) 
            else:
                logging.warning(f"Skipping bitrate calculation for '{original_title}' due to non-positive effective_runtime ({effective_runtime_for_bitrate}min) or total_size_gb ({total_size_gb:.3f}GB). Bitrate set to 0.")
                bitrate = 0 
            
            # --- ADDED DEBUG LOGGING ---
            if original_title == "The.Handmaids.Tale.S03.SweSub.1080p.x264-Justiso":
                logging.info(f"[FILTER_DEBUG HANDMAIDS] Pre-final bitrate assignment for '{original_title}':")
                logging.info(f"  total_size_gb (from scraper): {total_size_gb:.4f}")
                logging.info(f"  provides_per_item_size: {provides_per_item_size}")
                logging.info(f"  is_identified_as_pack: {is_identified_as_pack}")
                logging.info(f"  num_episodes_in_pack (calc'd in filter_results): {num_episodes_in_pack}")
                logging.info(f"  runtime (base for one item): {runtime}")
                logging.info(f"  actual_ep_count_for_bitrate_calc: {actual_ep_count_for_bitrate_calc}")
                logging.info(f"  effective_runtime_for_bitrate: {effective_runtime_for_bitrate:.2f}")
                logging.info(f"  calculated bitrate (to be assigned to result['bitrate']): {bitrate:.2f} Kbps")
            # --- END ADDED DEBUG LOGGING ---
            result['bitrate'] = bitrate
            # Store the count that reflects the content of total_size_gb
            result['num_episodes_in_pack_calculated'] = num_episodes_in_pack 

            
            #logging.debug(f"Filtering '{original_title}': Effective Size for Filter: {result['size']:.2f}GB (Total: {result['total_size_gb']:.2f}GB, Calc'd Episodes in Pack: {num_episodes_in_pack}), Bitrate: {bitrate:.2f}Mbps")
            
            # Add to pre-size filtered results
            pre_size_filtered_results.append(result.copy()) 
            
            # Size filters
            if result['size'] > 0:
                if result['size'] < min_size_gb:
                    size_type_msg = "Average episode size" if is_episode and is_identified_as_pack and num_episodes_in_pack > 0 else "Size"
                    result['filter_reason'] = f"{size_type_msg} too small: {result['size']:.2f} GB (min: {min_size_gb} GB)"
                    logging.info(f"Rejected: {size_type_msg} {result['size']:.2f}GB below minimum {min_size_gb}GB for '{original_title}' (Total pack: {result['total_size_gb']:.2f}GB)")
                    continue
                if result['size'] > max_size_gb:
                    size_type_msg = "Average episode size" if is_episode and is_identified_as_pack and num_episodes_in_pack > 0 else "Size"
                    result['filter_reason'] = f"Size too large: {result['size']:.2f} GB (max: {max_size_gb} GB)"
                    logging.info(f"Rejected: {size_type_msg} {result['size']:.2f}GB above maximum {max_size_gb}GB for '{original_title}' (Total pack: {result['total_size_gb']:.2f}GB)")
                    continue
            #logging.debug("✓ Passed size checks")
            
            # Bitrate filters
            min_bitrate_mbps = float(version_settings.get('min_bitrate_mbps', 0.0))
            max_bitrate_mbps = float(version_settings.get('max_bitrate_mbps', float('inf')) or float('inf'))
            
            if result.get('bitrate', 0) > 0:
                bitrate_mbps = result['bitrate'] / 1000  # Convert Kbps to Mbps for comparison
                if bitrate_mbps < min_bitrate_mbps:
                    result['filter_reason'] = f"Bitrate too low: {bitrate_mbps:.2f} Mbps (min: {min_bitrate_mbps} Mbps)"
                    logging.info(f"Rejected: Bitrate {bitrate_mbps:.2f}Mbps below minimum {min_bitrate_mbps}Mbps for '{original_title}' (Size: {result['size']:.2f}GB)")
                    continue
                if bitrate_mbps > max_bitrate_mbps:
                    result['filter_reason'] = f"Bitrate too high: {bitrate_mbps:.2f} Mbps (max: {max_bitrate_mbps} Mbps)"
                    logging.info(f"Rejected: Bitrate {bitrate_mbps:.2f}Mbps above maximum {max_bitrate_mbps}Mbps for '{original_title}' (Size: {result['size']:.2f}GB)")
                    continue
            #logging.debug("✓ Passed bitrate checks")
            
            # --- NEW: Pre-Normalization Filter Out Check ---
            # Check filter_out patterns against original fields BEFORE normalization
            if filter_out_patterns:
                original_fields_to_check = [original_title, filename, binge_group]
                matched_pre_norm_pattern = None
                for pattern in filter_out_patterns:
                    for field_value in original_fields_to_check:
                        # Check only if field_value exists (is not None or empty string)
                        if field_value and smart_search(pattern, field_value):
                            matched_pre_norm_pattern = pattern
                            break # Found a match for this pattern, stop checking fields
                    if matched_pre_norm_pattern:
                        break # Found a matching pattern, stop checking patterns

                if matched_pre_norm_pattern:
                    result['filter_reason'] = f"Matching filter_out pattern(s) before normalization: {matched_pre_norm_pattern}"
                    logging.info(f"Rejected (pre-norm): Matched filter_out pattern '{matched_pre_norm_pattern}' for '{original_title}' (Size: {result['size']:.2f}GB)")
                    continue
            # --- End NEW Pre-Normalization Check ---

            # --- Existing Pattern Matching (using normalized fields) ---
            normalized_filter_title = normalize_title(original_title).lower()
            normalized_filename = normalize_title(filename).lower() if filename else None
            normalized_binge_group = normalize_title(binge_group).lower() if binge_group else None

            # Function to check patterns against multiple fields
            def check_patterns(patterns, fields_to_check):
                matched = []
                for pattern in patterns:
                    for field_value in fields_to_check:
                        if field_value and smart_search(pattern, field_value):
                            matched.append(pattern)
                            break # Stop checking fields for this pattern once matched
                return matched

            fields_to_check_patterns = [normalized_filter_title, normalized_filename, normalized_binge_group]
            
            # Filter Out Check (on normalized fields - keep this as well)
            if filter_out_patterns:
                # Note: This check now runs *after* the pre-normalization check
                matched_out_patterns = check_patterns(filter_out_patterns, fields_to_check_patterns)
                if matched_out_patterns:
                    # Only reject if it wasn't already rejected by the pre-norm check
                    # (This check is now slightly redundant for patterns caught pre-norm, but harmless)
                    result['filter_reason'] = f"Matching filter_out pattern(s) after normalization: {', '.join(matched_out_patterns)}"
                    logging.info(f"Rejected (post-norm): Matched filter_out patterns '{matched_out_patterns}' for '{original_title}' (Size: {result['size']:.2f}GB)")
                    continue

            # Filter In Check (on normalized fields - keep this)
            if filter_in_patterns:
                matched_in_patterns = check_patterns(filter_in_patterns, fields_to_check_patterns)
                if not matched_in_patterns: # Reject if NO patterns matched ANY field
                    result['filter_reason'] = "Not matching any filter_in patterns (post-normalization)"
                    logging.info(f"Rejected (post-norm): No matching filter_in patterns for '{original_title}' (Size: {result['size']:.2f}GB)")
                    continue
            # logging.debug("✓ Passed pattern checks")
            # --- End Existing Pattern Matching ---

            # --- Adult Content Check (Uses original title and filename - No change needed) ---
            fields_to_check_adult = [original_title] # Start with original title
            if filename: fields_to_check_adult.append(filename)
            # Don't check bingeGroup for adult terms, too prone to false positives

            if adult_pattern and any(adult_pattern.search(field) for field in fields_to_check_adult if field):
                result['filter_reason'] = "Adult content filtered"
                logging.info(f"Rejected: Adult content detected for '{original_title}' (Size: {result['size']:.2f}GB)")
                continue
            # logging.debug("✓ Passed adult content check")
            # --- End Adult Content Check ---
            
            filtered_results.append(result)
            logging.info(f"Accepted: '{original_title}' (Size: {result['size']:.2f}GB)")
            
        except Exception as e:
            logging.error(f"Error filtering result '{original_title}': {str(e)}", exc_info=True)
            result['filter_reason'] = f"Error during filtering: {str(e)}"
            continue
    
    #logging.debug(f"\nFiltering complete: {len(filtered_results)}/{len(results)} results passed")
    return filtered_results, pre_size_filtered_results

def get_resolution_value(resolution: str) -> int:
    """Convert a resolution string to a numeric value for comparison."""
    resolution_order = {
        '2160p': 2160, '4k': 2160, 'uhd': 2160,
        '1440p': 1440, 'qhd': 1440,
        '1080p': 1080, 'fhd': 1080,
        '720p': 720, 'hd': 720,
        '480p': 480, 'sd': 480,
        '360p': 360
    }
    return resolution_order.get(resolution.lower(), 0)

def resolution_filter(result_resolution: str, max_resolution: str, resolution_wanted: str) -> bool:
    """
    Filter resolutions based on comparison operators.
    
    Args:
        result_resolution: The resolution of the result being checked
        max_resolution: The resolution to compare against
        resolution_wanted: The comparison operator ('<=', '==', or '>=')
        
    Returns:
        bool: Whether the result matches the resolution criteria
    """
    result_val = get_resolution_value(result_resolution)
    max_val = get_resolution_value(max_resolution)
    
    # Special handling for unknown resolutions
    if result_val == 0:
        # If resolution is unknown, check if it's likely a WEBRip or older content
        if result_resolution.lower() == 'unknown':
            # For unknown resolutions, we'll be lenient and assume it's SD/480p quality
            # This helps with older content where resolution isn't explicitly stated
            result_val = get_resolution_value('480p')
            #logging.debug(f"Unknown resolution detected - assuming SD/480p quality for filtering")
    
    # If max_val is 0 (invalid resolution), default to blocking
    if max_val == 0:
        #logging.debug(f"Invalid maximum resolution value: {max_resolution}")
        return False
        
    if resolution_wanted == '<=':
        return result_val <= max_val
    elif resolution_wanted == '==':
        # If the result resolution is unknown (value 0 or 480 after adjustment),
        # it cannot be strictly equal to a specific target resolution like 1080 or 2160.
        # The check `result_val == max_val` will handle this correctly.
        # If result_val is 480 (from unknown) and max_val is 2160, 480 == 2160 is false.
        # If result_val is 2160 and max_val is 2160, 2160 == 2160 is true.
        return result_val == max_val
    elif resolution_wanted == '>=':
        return result_val >= max_val
        
    return False