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
from datetime import datetime, timezone
# --- Import DirectAPI if type hinting is desired, ensure it's available in the execution path ---
# from cli_battery.app.direct_api import DirectAPI # Or adjust path as needed

def detect_language_codes(text: str) -> List[str]:
    """
    Fallback function to detect language/country codes in a title or alias.
    This is used as a fallback when PTT parsing fails.
    Returns a list of detected codes (e.g., ['UK', 'US', 'AU']).
    """
    if not text:
        return []
    
    # Common language/country codes that appear in titles
    language_codes = {
        'UK', 'US', 'AU', 'CA', 'NZ', 'DE', 'FR', 'ES', 'IT', 'NL', 'SE', 'NO', 'DK', 'FI',
        'PL', 'CZ', 'HU', 'RO', 'BG', 'HR', 'RS', 'SI', 'SK', 'EE', 'LV', 'LT', 'PT', 'GR',
        'JP', 'KR', 'CN', 'IN', 'BR', 'MX', 'AR', 'CL', 'PE', 'CO', 'VE', 'EC', 'BO', 'PY',
        'UY', 'GY', 'SR', 'GF', 'FK', 'GS', 'IO', 'PN', 'TC', 'VG', 'AI', 'BM', 'KY', 'MS',
        'KN', 'LC', 'VC', 'AG', 'DM', 'GD', 'TT', 'BB', 'JM', 'HT', 'DO', 'PR', 'CU', 'JM',
        'BS', 'TC', 'AW', 'CW', 'SX', 'BQ', 'BL', 'MF', 'GP', 'MQ', 'RE', 'YT', 'NC', 'PF',
        'WF', 'TF', 'PM', 'ST', 'CV', 'GM', 'GN', 'GW', 'SL', 'LR', 'CI', 'BF', 'ML', 'NE',
        'TD', 'SD', 'ER', 'DJ', 'SO', 'KE', 'TZ', 'UG', 'RW', 'BI', 'CD', 'CG', 'GA', 'GQ',
        'ST', 'AO', 'ZM', 'ZW', 'BW', 'NA', 'SZ', 'LS', 'MG', 'MU', 'SC', 'KM', 'YT', 'RE',
        'MZ', 'MW', 'ZW', 'ZM', 'TZ', 'KE', 'UG', 'RW', 'BI', 'CD', 'CG', 'GA', 'GQ', 'ST',
        'AO', 'ZM', 'ZW', 'BW', 'NA', 'SZ', 'LS', 'MG', 'MU', 'SC', 'KM', 'YT', 'RE', 'MZ',
        'MW', 'ZW', 'ZM', 'TZ', 'KE', 'UG', 'RW', 'BI', 'CD', 'CG', 'GA', 'GQ', 'ST', 'AO'
    }
    
    # Split text into words and check for language codes
    words = text.upper().split()
    detected_codes = []
    
    for word in words:
        # Remove common punctuation that might be attached to codes
        clean_word = re.sub(r'[^\w]', '', word)
        if clean_word in language_codes:
            detected_codes.append(clean_word)
    
    return detected_codes

def extract_year_from_title(title: str) -> Optional[int]:
    """
    Simple year extraction from title when PTT fails to parse it.
    Looks for 4-digit years and returns the earliest one found.
    """
    if not title:
        return None
    
    # Find all 4-digit numbers that could be years (1900-2099)
    year_pattern = r'\b(19[0-9]{2}|20[0-9]{2})\b'
    years = re.findall(year_pattern, title)
    
    if years:
        # Convert to integers and return the earliest year
        year_ints = [int(year) for year in years]
        earliest_year = min(year_ints)
        logging.debug(f"Extracted year {earliest_year} from title: '{title}' (found years: {year_ints})")
        return earliest_year
    
    return None

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

    # Initialize caches for this filter_results call
    _season_year_cache = {}  # Cache for season year lookups: {(imdb_id, season): year}
    _show_metadata_cache = {}  # Cache for show metadata: {imdb_id: metadata}
    _aliases_cache = {}  # Cache for aliases: {imdb_id: aliases}

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
    
    # --- Language Code Filtering Setup ---
    # We'll check for language codes after API aliases are fetched for each result
    should_filter_language = False
    expected_language_code = None
    detected_codes_in_original = []
    # --- End Language Code Filtering Setup ---
    
    # Determine base similarity threshold
    # Override anime similarity threshold to be more restrictive to prevent false matches
    original_anime_setting = version_settings.get('similarity_threshold_anime', 0.60)
    anime_threshold = max(0.80, float(original_anime_setting))
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
            
            # Get parsed info from result (should be already parsed by PTT)
            if not parsed_info:
                result['filter_reason'] = "Missing parsed info"
                logging.info(f"Rejected: Missing parsed info for '{original_title}' (Size: {result['size']:.2f}GB)")
                continue
            
            # DEBUG: Add detailed logging for Frasier-like cases to see what PTT parsed
            if "frasier" in original_title.lower() or "1993" in original_title or "2004" in original_title:
                logging.info(f"DEBUG FRASIER PTT: '{original_title}'")
                logging.info(f"  - parsed_info: {parsed_info}")
                logging.info(f"  - parsed year: {parsed_info.get('year')}")
                logging.info(f"  - parsed seasons: {parsed_info.get('season_episode_info', {}).get('seasons')}")
                logging.info(f"  - parsed episodes: {parsed_info.get('season_episode_info', {}).get('episodes')}")
            
            # Check if it's marked as trash by PTT and filter_trash_releases is enabled
            filter_trash_releases = get_setting('Scraping', 'filter_trash_releases', True)
            if filter_trash_releases and parsed_info.get('trash', False):
                result['filter_reason'] = "Marked as trash by parser"
                logging.info(f"Rejected: Marked as trash by parser for '{original_title}' (Size: {result['size']:.2f}GB)")
                continue
            
            # Language code filtering will be done after API aliases are fetched
            
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
            # logging.debug(f"  - Parsed info title: '{parsed_title_str}', parsed_info keys: {list(parsed_info.keys()) if parsed_info else 'None'}")
            normalized_parsed_title = normalize_title(parsed_title_str).lower() if parsed_title_str else None

            # Create simplified versions by removing all punctuation and spaces (used by all similarity checks)
            simple_query = re.sub(r'[^a-z0-9]', '', normalized_query_title)
            simple_result = re.sub(r'[^a-z0-9]', '', normalized_result_title)
            simple_parsed = re.sub(r'[^a-z0-9]', '', normalized_parsed_title) if normalized_parsed_title else None
            
            # --- Main Title Similarity ---
            main_sim_set = fuzz.token_set_ratio(normalized_result_title, normalized_query_title) / 100.0
            if normalized_parsed_title:
                main_sim_sort = fuzz.token_sort_ratio(normalized_parsed_title, normalized_query_title) / 100.0
                main_title_sim = (main_sim_set + main_sim_sort) / 2.0
            else:
                main_title_sim = main_sim_set

            # Apply penalty for titles that are much longer than the query (e.g., "Dragon Ball" vs "Dragon Ball Daima")
            # This prevents false positives when the result title contains the query plus significant additional content
            query_length = len(normalized_query_title)

            # Store original values for debug logging
            original_main_sim_set = main_sim_set
            original_main_sim_sort = main_sim_sort

            # Apply penalty to token_set_ratio (result vs query comparison)
            result_length = len(normalized_result_title)
            token_set_penalty_applied = False
            if query_length > 0 and result_length > query_length * 1.5:
                length_ratio = query_length / result_length
                # Use a more reasonable penalty: reduce similarity proportionally to length difference
                penalty_factor = max(0.3, length_ratio)  # Minimum 30% of original similarity
                main_sim_set *= penalty_factor
                token_set_penalty_applied = True

            # Apply penalty to token_sort_ratio (parsed vs query comparison)
            token_sort_penalty_applied = False
            if normalized_parsed_title and len(normalized_parsed_title) > query_length * 1.5:
                parsed_length = len(normalized_parsed_title)
                parsed_ratio = query_length / parsed_length
                parsed_penalty_factor = max(0.3, parsed_ratio)  # Minimum 30% of original similarity
                main_sim_sort *= parsed_penalty_factor
                token_sort_penalty_applied = True

                # Recalculate main_title_sim with penalized components
                main_title_sim = (main_sim_set + main_sim_sort) / 2.0
            else:
                # No parsed title, main_title_sim is just main_sim_set
                main_title_sim = main_sim_set

            # Special handling for common acronym variations (like SHIELD)
            # Only apply if we haven't already applied a length penalty (to avoid overriding penalties)
            if normalized_parsed_title:
                original_main_title_sim = (original_main_sim_set + original_main_sim_sort) / 2.0
            else:
                original_main_title_sim = original_main_sim_set
            penalty_was_applied = token_set_penalty_applied or token_sort_penalty_applied

            # Initialize variables for debug logging
            simple_sim_result = 0.0
            simple_sim_parsed = 0.0 if simple_parsed else None
            simple_sim = 0.0

            if main_title_sim < 0.8 and not penalty_was_applied:
                # Check if this might be an acronym mismatch (like S.H.I.E.L.D. vs S H I E L D)
                simple_sim_result = fuzz.ratio(simple_result, simple_query) / 100.0
                if simple_parsed:
                    simple_sim_parsed = fuzz.ratio(simple_parsed, simple_query) / 100.0
                    simple_sim = max(simple_sim_result, simple_sim_parsed)
                else:
                    simple_sim = simple_sim_result

                # Use the better score, but don't let it exceed reasonable bounds
                main_title_sim = max(main_title_sim, min(simple_sim, 0.95))
                logging.debug(f"  - Acronym handling applied: simple_sim: {simple_sim:.3f}, new main_title_sim: {main_title_sim:.3f}")
            elif main_title_sim < 0.8 and penalty_was_applied:
                pass


            # --- Alias Similarities ---
            alias_similarities = []
            if normalized_aliases:
                for alias in normalized_aliases:
                    # Compare parsed title against alias when available, otherwise query against alias
                    if normalized_parsed_title:
                        alias_sim_set = fuzz.token_set_ratio(normalized_parsed_title, alias) / 100.0
                        alias_sim_sort = fuzz.token_sort_ratio(normalized_parsed_title, alias) / 100.0
                        alias_sim = (alias_sim_set + alias_sim_sort) / 2.0
                    else:
                        alias_sim_set = fuzz.token_set_ratio(normalized_query_title, alias) / 100.0
                        alias_sim = alias_sim_set

                    # Apply same acronym handling for aliases
                    if alias_sim < 0.8:
                        simple_alias = re.sub(r'[^a-z0-9]', '', alias)
                        # Compare query vs alias for acronym handling, not result vs alias
                        simple_sim_result = fuzz.ratio(simple_query, simple_alias) / 100.0
                        if simple_parsed:
                            simple_sim_parsed = fuzz.ratio(simple_query, simple_alias) / 100.0
                            simple_sim = max(simple_sim_result, simple_sim_parsed)
                        else:
                            simple_sim = simple_sim_result
                        alias_sim = max(alias_sim, min(simple_sim, 0.95))

                    # Apply length penalty for aliases that are much longer than the comparison title
                    # This penalizes when the alias being compared is significantly longer than the parsed/query title
                    alias_length = len(alias)
                    comparison_length = len(normalized_parsed_title) if normalized_parsed_title else len(normalized_query_title)
                    if alias_length > 0 and alias_length > comparison_length * 1.5:
                        alias_ratio = comparison_length / alias_length
                        alias_penalty_factor = max(0.3, alias_ratio)  # Minimum 30% of original similarity
                        alias_original_sim = alias_sim
                        alias_sim *= alias_penalty_factor

                    alias_similarities.append(alias_sim)

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
                
                # Apply same acronym handling for translated titles
                if translated_title_sim < 0.8:
                    simple_translated = re.sub(r'[^a-z0-9]', '', normalized_translated_title)
                    simple_sim_result = fuzz.ratio(simple_result, simple_translated) / 100.0
                    if simple_parsed:
                        simple_sim_parsed = fuzz.ratio(simple_parsed, simple_translated) / 100.0
                        simple_sim = max(simple_sim_result, simple_sim_parsed)
                    else:
                        simple_sim = simple_sim_result
                    translated_title_sim = max(translated_title_sim, min(simple_sim, 0.95))

                # Apply length penalty for translated titles that are much longer than the query
                # This penalizes when the translated title being compared is significantly longer than the query title
                trans_length = len(normalized_translated_title)
                query_length = len(normalized_query_title)
                if trans_length > 0 and trans_length > query_length * 1.5:
                    trans_ratio = query_length / trans_length
                    trans_penalty_factor = max(0.3, trans_ratio)  # Minimum 30% of original similarity
                    trans_original_sim = translated_title_sim
                    translated_title_sim *= trans_penalty_factor

            # Compute initial best similarity score (without API aliases)
            best_sim = max(main_title_sim, best_alias_sim, translated_title_sim)

            # --- Fetch additional aliases via DirectAPI ---
            item_aliases = {}
            
            # Check cache first for aliases
            if imdb_id in _aliases_cache:
                item_aliases = _aliases_cache[imdb_id]
                # logging.debug(f"Using cached aliases for {imdb_id}")
            else:
                try:
                    if direct_api:
                        if content_type.lower() == 'movie':
                            item_aliases, _ = direct_api.get_movie_aliases(imdb_id)
                        else:
                            item_aliases, _ = direct_api.get_show_aliases(imdb_id)
                except Exception as alias_err:
                    logging.warning(f"Failed to fetch aliases for {imdb_id}: {alias_err}")
                    item_aliases = {}

                # Cache the result (even if empty or failed)
                _aliases_cache[imdb_id] = item_aliases

            # Ensure item_aliases is a dictionary even if the API returned None or an unexpected value
            if not isinstance(item_aliases, dict):
                item_aliases = {}

            # -------------------------------------------------------------
            # Include original_title from metadata in alias pool
            # -------------------------------------------------------------
            try:
                if imdb_id and direct_api:
                    # Check metadata cache first
                    if imdb_id in _show_metadata_cache:
                        meta_data = _show_metadata_cache[imdb_id]
                        # logging.debug(f"Using cached metadata for original_title lookup for {imdb_id}")
                    else:
                        if content_type.lower() == 'movie':
                            meta_data, _ = direct_api.get_movie_metadata(imdb_id)
                        else:
                            meta_data, _ = direct_api.get_show_metadata(imdb_id)
                        # Cache the result
                        _show_metadata_cache[imdb_id] = meta_data

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
            
            # --- Language Code Detection (after API aliases are fetched) ---
            # Check for language codes in original title, matching_aliases, and API aliases
            all_titles_to_check = [title]
            if matching_aliases:
                all_titles_to_check.extend(matching_aliases)
            
            # Add API aliases to the check
            for alias_list in item_aliases.values():
                all_titles_to_check.extend(alias_list)
            
            detected_codes_in_original = []
            for check_title in all_titles_to_check:
                # Use PTT parser to detect country codes in titles/aliases
                try:
                    from scraper.functions.ptt_parser import parse_with_ptt
                    parsed_alias = parse_with_ptt(check_title)
                    if parsed_alias.get('country'):
                        country_code_mapping = {'gb': 'UK', 'us': 'US', 'au': 'AU', 'ca': 'CA', 'nz': 'NZ'}
                        detected_code = country_code_mapping.get(parsed_alias['country'].lower(), parsed_alias['country'].upper())
                        detected_codes_in_original.append(detected_code)
                except Exception as e:
                    # Fallback to our custom detection if PTT parsing fails
                    codes = detect_language_codes(check_title)
                    detected_codes_in_original.extend(codes)
            
            # Remove duplicates while preserving order
            detected_codes_in_original = list(dict.fromkeys(detected_codes_in_original))
            
            if detected_codes_in_original:
                should_filter_language = True
                expected_language_code = detected_codes_in_original[0]  # Use first detected code
                logging.info(f"Language code filtering enabled. Expected code: {expected_language_code}, detected in original/aliases: {detected_codes_in_original}")
            else:
                should_filter_language = False
                expected_language_code = None
                # logging.info(f"No language codes detected in original title or aliases. Language code filtering disabled.")
            # --- End Language Code Detection ---

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
            if item_aliases:
                pass
            for alias_list in item_aliases.values():
                for alias in alias_list:
                    normalized_api_alias = normalize_title(alias).lower()
                    # Compare parsed title against API alias when available, otherwise query against API alias
                    if normalized_parsed_title:
                        alias_sim_set = fuzz.token_set_ratio(normalized_parsed_title, normalized_api_alias) / 100.0
                        alias_sim_sort = fuzz.token_sort_ratio(normalized_parsed_title, normalized_api_alias) / 100.0
                        final_alias_sim = (alias_sim_set + alias_sim_sort) / 2.0
                    else:
                        alias_sim_set = fuzz.token_set_ratio(normalized_query_title, normalized_api_alias) / 100.0
                        final_alias_sim = alias_sim_set

                    # Apply same acronym handling for API aliases
                    if final_alias_sim < 0.8:
                        simple_api_alias = re.sub(r'[^a-z0-9]', '', normalized_api_alias)
                        # Compare query vs API alias for acronym handling, not result vs API alias
                        simple_sim_result = fuzz.ratio(simple_query, simple_api_alias) / 100.0
                        if simple_parsed:
                            simple_sim_parsed = fuzz.ratio(simple_query, simple_api_alias) / 100.0
                            simple_sim = max(simple_sim_result, simple_sim_parsed)
                        else:
                            simple_sim = simple_sim_result
                        final_alias_sim = max(final_alias_sim, min(simple_sim, 0.95))

                    # Apply length penalty when parsed title is longer than API alias OR contains different content
                    # This penalizes when the parsed title contains additional/different content beyond the alias
                    # (e.g., "Dragon Ball Daima" vs alias "Dragon Ball" or "Dragon Ball 1986" - indicates a different show)
                    api_alias_length = len(normalized_api_alias)
                    comparison_length = len(normalized_parsed_title) if normalized_parsed_title else len(normalized_query_title)

                    # Check if parsed title is longer than the API alias (lowered threshold to 1.2x for better detection)
                    # Also check for significant word differences even at similar lengths
                    if api_alias_length > 0 and comparison_length > api_alias_length * 1.2:
                        length_ratio = api_alias_length / comparison_length
                        api_alias_penalty_factor = max(0.3, length_ratio)  # Minimum 30% of original similarity
                        api_alias_original_sim = final_alias_sim
                        final_alias_sim *= api_alias_penalty_factor

                    # Additional check: If titles are similar length but have different non-year content, apply penalty
                    # This catches cases like "dragon.ball.daima" vs "dragon.ball.1986" or "dragon.ball.z" vs "dragon.ball"
                    elif api_alias_length > 0 and final_alias_sim > 0.7:
                        # Extract non-numeric words from both titles to check for content differences
                        # Include single-character words (like 'Z', 'X', 'GT') which are significant in anime titles
                        alias_words = set(w.lower() for w in normalized_api_alias.split('.') if not w.isdigit() and w)
                        parsed_words = set(w.lower() for w in normalized_parsed_title.split('.') if not w.isdigit() and w)

                        # Check if there are significant word differences (excluding years)
                        unique_to_parsed = parsed_words - alias_words
                        unique_to_alias = alias_words - parsed_words

                        # Special handling for known problematic series with very similar names
                        # Dragon Ball series: original, Z, GT, Super, Daima are all different shows
                        is_dragon_ball_variant = 'dragon' in alias_words and 'ball' in alias_words
                        if is_dragon_ball_variant and (unique_to_parsed or unique_to_alias):
                            # Apply severe penalty for Dragon Ball variants
                            content_penalty = 0.3  # Reduce similarity by 70% for different Dragon Ball series
                            api_alias_original_sim = final_alias_sim
                            final_alias_sim *= content_penalty
                        elif unique_to_parsed or unique_to_alias:
                            # Apply standard penalty for different content even at similar lengths
                            content_penalty = 0.5  # Reduce similarity by 50% for different content
                            api_alias_original_sim = final_alias_sim
                            final_alias_sim *= content_penalty

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
            else:
                best_sim = max(main_title_sim, best_alias_sim, translated_title_sim)


            # -------------------------------------------------------------
            # Check if the best similarity meets the threshold (now updated)
            # -------------------------------------------------------------
            
            # --- ANIME-SPECIFIC SANITY CHECK ---
            # For anime, add additional validation to prevent false matches from fuzzy token overlap
            if is_anime and best_sim >= similarity_threshold:
                # logging.info(f"DEBUG SANITY: Running anime sanity check for '{original_title}' (best_sim={best_sim:.3f}, threshold={similarity_threshold:.3f})")
                
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
                
                # logging.info(f"DEBUG SANITY: char_overlap={char_overlap_ratio:.3f}, word_match={word_match_ratio:.3f} for '{original_title}'")
                
                # Require either strong character overlap OR meaningful word matches for anime
                if char_overlap_ratio < 0.4 and word_match_ratio < 0.5:
                    result['filter_reason'] = f"Anime title failed sanity check (char_overlap={char_overlap_ratio:.2f}, word_match={word_match_ratio:.2f}, similarity={best_sim:.2f})"
                    logging.info(f"Rejected: Anime sanity check failed for '{original_title}' - insufficient substantial overlap despite fuzzy similarity {best_sim:.2f} (Size: {result['size']:.2f}GB)")
                    continue

            # --- DEBUG: Log detailed similarity scores for troublesome titles ---
            should_debug = ("araiguma" in original_title.lower() or "calcal" in original_title.lower() or
                          "shield" in original_title.lower() or "s.h.i.e.l.d" in title.lower())
            
            if best_sim < similarity_threshold:
                # Log the failure reason including all comparison scores
                result['filter_reason'] = f"Title similarity too low (best={best_sim:.2f} < {similarity_threshold})"
                logging.info(f"Rejected: Title similarity too low (best={best_sim:.2f} < {similarity_threshold}) for '{original_title}' (Size: {result['size']:.2f}GB)")
                continue
            # else:
            
            # --- Language Code Filtering (after similarity check passes) ---
            if should_filter_language:
                # Original/alias HAS language code - filter out different codes, derank missing codes
                result_country = parsed_info.get('country')
                
                # Convert PTT country codes to our expected format
                country_code_mapping = {'gb': 'UK', 'us': 'US', 'au': 'AU', 'ca': 'CA', 'nz': 'NZ'}
                result_language_code = None
                if result_country:
                    result_language_code = country_code_mapping.get(result_country.lower(), result_country.upper())
                
                if result_language_code:
                    # Result has language codes - check if they match expected
                    if expected_language_code != result_language_code:
                        # Filter out results with different language codes
                        result['filter_reason'] = f"Language code mismatch: expected {expected_language_code}, found {result_language_code}"
                        logging.info(f"Rejected: Language code mismatch for '{original_title}' - expected {expected_language_code}, found {result_language_code} (Size: {result['size']:.2f}GB)")
                        continue
                    else:
                        logging.info(f"Language code match for '{original_title}': {result_language_code} matches expected {expected_language_code}")
                else:
                    # Result has no language codes but original does - apply significant ranking penalty
                    result['language_code_missing_penalty'] = -500
                    result['language_code_expected'] = expected_language_code
                    logging.info(f"Missing language code penalty applied for '{original_title}' - expected {expected_language_code}, found none")
            else:
                # Original/alias has NO language code - prefer items without language codes, but accept all
                result_country = parsed_info.get('country')
                country_code_mapping = {'gb': 'UK', 'us': 'US', 'au': 'AU', 'ca': 'CA', 'nz': 'NZ'}
                result_language_code = None
                if result_country:
                    result_language_code = country_code_mapping.get(result_country.lower(), result_country.upper())
                
                if result_language_code:
                    # Result has language codes but original doesn't - apply small ranking penalty
                    result['has_language_codes'] = True
                    result['detected_language_codes'] = [result_language_code]
                    result['language_code_unexpected_penalty'] = -100  # Smaller penalty for unexpected language codes
                    logging.info(f"Result has language codes but original doesn't - will be ranked lower: '{original_title}' - codes: {result_language_code}")
                else:
                    # Mark this result as not having language codes for ranking preference
                    result['has_language_codes'] = False
            # --- End Language Code Filtering ---
            
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
                    
                    # If PTT didn't parse a year, try our own simple extraction
                    if parsed_year is None:
                        extracted_year = extract_year_from_title(original_title)
                        if extracted_year:
                            parsed_year = extracted_year
                            logging.info(f"PTT didn't parse year, extracted {extracted_year} from title: '{original_title}'")
                    
                    if parsed_year:
                        # For TV shows, we should compare against the season's air date, not the original show premiere
                        # Get the season-specific year if available, otherwise fall back to the original year
                        target_year = year  # Default to original year
                        
                        # Try to get the season-specific year from the database
                        if imdb_id and season is not None:
                            # Check cache first
                            cache_key = (imdb_id, season)
                            if cache_key in _season_year_cache:
                                target_year = _season_year_cache[cache_key]
                                # logging.debug(f"Using cached season year for {imdb_id} S{season}: {target_year}")
                            else:
                                try:
                                    from database.database_reading import get_season_year
                                    season_year = get_season_year(imdb_id=imdb_id, season_number=season)
                                    if season_year:
                                        target_year = season_year
                                        _season_year_cache[cache_key] = season_year  # Cache the result
                                        logging.info(f"Using season {season} air date year ({season_year}) instead of original show year ({year}) for '{original_title}'")
                                    else:
                                        # Fallback: Try to get season year from metadata API
                                        logging.debug(f"No season year in database for {imdb_id} S{season}, trying metadata API fallback")
                                        if direct_api:
                                            # Check metadata cache first
                                            if imdb_id in _show_metadata_cache:
                                                show_metadata = _show_metadata_cache[imdb_id]
                                                # logging.debug(f"Using cached show metadata for {imdb_id}")
                                            else:
                                                try:
                                                    show_metadata, _ = direct_api.get_show_metadata(imdb_id)
                                                    _show_metadata_cache[imdb_id] = show_metadata  # Cache the result
                                                except Exception as api_err:
                                                    logging.warning(f"Error getting show metadata for {imdb_id}: {api_err}")
                                                    show_metadata = None
                                                    _show_metadata_cache[imdb_id] = None  # Cache the failure
                                            
                                            if show_metadata and isinstance(show_metadata, dict):
                                                trakt_seasons_data = show_metadata.get('seasons')
                                                if isinstance(trakt_seasons_data, dict) and season in trakt_seasons_data:
                                                    current_season_trakt_data = trakt_seasons_data[season]
                                                    if isinstance(current_season_trakt_data, dict) and 'episodes' in current_season_trakt_data:
                                                        episodes_dict_for_season = current_season_trakt_data['episodes']
                                                        if episodes_dict_for_season:
                                                            # Get the first episode's air date to determine season year
                                                            first_episode_key = min(episodes_dict_for_season.keys(), key=lambda x: int(x) if str(x).isdigit() else float('inf'))
                                                            first_episode_data = episodes_dict_for_season[first_episode_key]
                                                            if isinstance(first_episode_data, dict) and 'first_aired' in first_episode_data:
                                                                air_date_full_utc_str = first_episode_data['first_aired']
                                                                if isinstance(air_date_full_utc_str, str) and air_date_full_utc_str:
                                                                    try:
                                                                        # Parse the UTC timestamp string
                                                                        if air_date_full_utc_str.endswith('Z'):
                                                                            air_date_full_utc_str = air_date_full_utc_str[:-1] + '+00:00'
                                                                        utc_dt = datetime.fromisoformat(air_date_full_utc_str)
                                                                        if utc_dt.tzinfo is None:
                                                                            utc_dt = utc_dt.replace(tzinfo=timezone.utc)
                                                                        season_year = utc_dt.year
                                                                        target_year = season_year
                                                                        _season_year_cache[cache_key] = season_year  # Cache the result
                                                                        logging.info(f"Using season {season} air date year ({season_year}) from metadata API instead of original show year ({year}) for '{original_title}'")
                                                                    except Exception as date_parse_err:
                                                                        logging.warning(f"Failed to parse season {season} air date from metadata API: {date_parse_err}")
                                    
                                    if target_year == year:  # If we still haven't found a season year
                                        _season_year_cache[cache_key] = year  # Cache the fallback
                                        logging.debug(f"No season-specific year found for {imdb_id} S{season}, using original show year ({year})")
                                except Exception as season_year_err:
                                    logging.warning(f"Error getting season year for {imdb_id} S{season}: {season_year_err}, using original show year ({year})")
                                    _season_year_cache[cache_key] = year  # Cache the fallback
                        
                        if isinstance(parsed_year, list):
                            # Ensure all elements in parsed_year are convertible to int before comparison
                            try:
                                # For TV shows, be more lenient with year matching
                                # Check if any year in the list matches with appropriate tolerance
                                year_matches = []
                                for py in parsed_year:
                                    if str(py).isdigit():
                                        py_int = int(py)
                                        year_difference = abs(py_int - target_year)
                                        
                                        # If the torrent year matches the original show year exactly, be more lenient
                                        if py_int == year and target_year != year:
                                            # Torrent uses original show year, but we have a different season year
                                            # Allow if the season year is within a reasonable range (e.g., ±5 years)
                                            max_year_difference = 5  # More lenient for TV shows
                                            if year_difference <= max_year_difference:
                                                year_matches.append(py_int)
                                                logging.info(f"Accepting torrent with original show year ({py_int}) for season {season} (air date: {target_year}) - within {max_year_difference} year tolerance for '{original_title}'")
                                        else:
                                            # Standard ±1 year tolerance for other cases
                                            if year_difference <= 1:
                                                year_matches.append(py_int)
                                
                                if not year_matches:
                                    result['filter_reason'] = f"Year mismatch: {parsed_year} (expected: {target_year}, original show: {year})"
                                    logging.info(f"Rejected: TV year list {parsed_year} doesn't match season year {target_year} for '{original_title}' (Size: {result['size']:.2f}GB)")
                                    continue
                            except ValueError:
                                # Handle cases where a year in the list is not a valid integer
                                logging.warning(f"Skipping year check due to invalid year format in list for '{original_title}': {parsed_year}")
                                result['filter_reason'] = f"Invalid year format in list: {parsed_year}"
                                continue

                        elif isinstance(parsed_year, (int, str)) and str(parsed_year).isdigit():
                            parsed_year_int = int(parsed_year)
                            
                            # For TV shows, be more lenient with year matching
                            # Many torrents incorrectly use the original show year instead of the season air date
                            year_difference = abs(parsed_year_int - target_year)
                            
                            # DEBUG: Add detailed logging for Frasier-like cases
                            if "frasier" in original_title.lower() or "1993" in original_title or "2004" in original_title:
                                logging.info(f"DEBUG FRASIER: '{original_title}'")
                                logging.info(f"  - parsed_year: {parsed_year} (int: {parsed_year_int})")
                                logging.info(f"  - original show year: {year}")
                                logging.info(f"  - target_year (season/fallback): {target_year}")
                                logging.info(f"  - year_difference: {year_difference}")
                                logging.info(f"  - parsed_year_int == year: {parsed_year_int == year}")
                                logging.info(f"  - target_year != year: {target_year != year}")
                            
                            # If the torrent year matches the original show year exactly, be more lenient
                            if parsed_year_int == year and target_year != year:
                                # Torrent uses original show year, but we have a different season year
                                # Allow if the season year is within a reasonable range (e.g., ±5 years)
                                max_year_difference = 5  # More lenient for TV shows
                                if year_difference <= max_year_difference:
                                    logging.info(f"Accepting torrent with original show year ({parsed_year_int}) for season {season} (air date: {target_year}) - within {max_year_difference} year tolerance for '{original_title}'")
                                else:
                                    result['filter_reason'] = f"Year mismatch: {parsed_year} (expected: {target_year}, original show: {year}) - torrent uses original show year but season air date is too far"
                                    logging.info(f"Rejected: TV year {parsed_year} (original show year) too far from season year {target_year} for '{original_title}' (Size: {result['size']:.2f}GB)")
                                    continue
                            else:
                                # Standard ±1 year tolerance for other cases
                                if year_difference > 1:
                                    result['filter_reason'] = f"Year mismatch: {parsed_year} (expected: {target_year}, original show: {year})"
                                    logging.info(f"Rejected: TV year {parsed_year} doesn't match season year {target_year} for '{original_title}' (Size: {result['size']:.2f}GB)")
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
                        # Add detailed logging to understand why this is considered a single episode
                        logging.info(f"SINGLE EPISODE DEBUG: '{original_title}' rejected because season_pack='N/A'")
                        logging.info(f"SINGLE EPISODE DEBUG: season_episode_info: {season_episode_info}")
                        logging.info(f"SINGLE EPISODE DEBUG: episodes: {season_episode_info.get('episodes', [])}")
                        logging.info(f"SINGLE EPISODE DEBUG: seasons: {season_episode_info.get('seasons', [])}")
                        logging.info(f"SINGLE EPISODE DEBUG: parsed_info: {parsed_info}")
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
                                # Always reject single-episode results when searching for multi-episode packs
                                # regardless of check_pack_wantedness setting
                                result['filter_reason'] = "Non-multi result when searching for multi"
                                logging.info(f"Rejected: Not enough episodes for multi mode for '{original_title}' (is_anime={is_anime}, heuristic_failed={not is_likely_anime_pack}) (Size: {result['size']:.2f}GB)")
                                continue
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
                             # BUT: For anime with XEM mapping, be more restrictive
                             # If we're searching for a specific season (not S1) and the torrent has no season info,
                             # we should be more cautious to avoid grabbing episodes from wrong seasons
                             if is_anime and season > 1:
                                 # For anime S2+, if no season info, be more restrictive
                                 # This prevents grabbing "Episode 07" from any season when we want S02E07
                                 # Only allow if we have absolute episode numbers or other strong indicators
                                 has_absolute_episode = result.get('target_abs_episode') is not None
                                 has_episode_in_title = bool(re.search(rf'\b{episode}\b', original_title))
                                 
                                 # Additional check: if the title only contains episode number without season,
                                 # and we're searching for a specific season (not S1), be more restrictive
                                 # This catches cases like "Dandadan - 07" when we want S02E07
                                 title_has_only_episode = (
                                     has_episode_in_title and 
                                     not re.search(r'[Ss]\d+', original_title) and  # No season info in title
                                     not has_absolute_episode  # No absolute episode number
                                 )
                                 
                                 if title_has_only_episode:
                                     season_match = False
                                     logging.info(f"Rejecting anime result with only episode number (no season/absolute) when searching for S{season}E{episode}: '{original_title}'")
                                 elif not has_absolute_episode and not has_episode_in_title:
                                     season_match = False
                                     logging.info(f"Rejecting anime result with no season info when searching for S{season}E{episode}: '{original_title}' (no absolute episode or strong episode indicator)")
                                 else:
                                     # Even if we have episode indicator, be more restrictive for S2+
                                     # Only allow if we have absolute episode numbers that provide proper context
                                     if has_absolute_episode:
                                         season_match = True
                                         lenient_season_pass = True
                                         logging.info(f"Allowing anime result with no season info but with absolute episode evidence for S{season}E{episode}: '{original_title}'")
                                     else:
                                         season_match = False
                                         logging.info(f"Rejecting anime result with episode indicator but no absolute episode evidence for S{season}E{episode}: '{original_title}'")
                             else:
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
                    is_potential_single_season_pack = False # Default to false

                    if not multi:
                        # Check for multi-season packs (e.g., "Complete", "S01,S02")
                        if (season_pack == 'Complete' or (season_pack not in ['N/A', 'Unknown'] and ',' in season_pack)):
                            result['filter_reason'] = "Multi-season pack when searching for single episode"
                            logging.info(f"Rejected: Multi-season pack in single episode mode for '{original_title}' (Size: {result['size']:.2f}GB)")
                            continue

                        # Also check if multiple distinct episodes are detected explicitly
                        if len(result_episodes) > 1:
                            result['filter_reason'] = f"Multiple episodes detected: {result_episodes} when searching for single episode {episode}"
                            logging.info(f"Rejected: Multiple episodes {result_episodes} in single episode mode for '{original_title}' (Size: {result['size']:.2f}GB)")
                            continue
                        
                        # This check is for single season packs and should apply only in single mode.
                        # It identifies torrents that are packs of the correct season but don't list episodes,
                        # which are then rejected if the specific episode isn't found later.
                        is_potential_single_season_pack = season_pack not in ['N/A', 'Unknown'] and not result_episodes
                    
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
                        
                                                 # Additional safety check for anime with XEM mapping: if we're searching for a specific season
                         # and the torrent has no season info, be more restrictive
                         # Debug logging to understand the condition
                         debug_condition = f"is_anime={is_anime}, season={season}, not result_seasons={not result_seasons}, lenient_season_pass={lenient_season_pass}"
                         logging.debug(f"Episode match safety check condition: {debug_condition} for '{original_title}'")
                         logging.debug(f"PTT parsed info for '{original_title}': result_seasons={result_seasons}, result_episodes={result_episodes}")
                         
                         # More comprehensive check: if we're searching for anime S2+ and the torrent has no season info
                         # but only episode numbers, be very restrictive
                         # Also check if the parsed season doesn't match what we're looking for
                         season_mismatch = result_seasons and season not in result_seasons
                         if is_anime and season > 1 and (not result_seasons or season_mismatch):
                             # This is the problematic case: anime S2+, no season info, but episode matches
                             # Check if this looks like a standalone episode without proper season context
                             title_has_only_episode = (
                                 not re.search(r'[Ss]\d+', original_title) and  # No season info in title
                                 result.get('target_abs_episode') is None  # No absolute episode number
                             )
                             
                             # For anime S2+ with season mismatch, always reject regardless of absolute episode numbers
                             # Absolute episode numbers should only be used when there's no season info at all
                             if season_mismatch:
                                 logging.info(f"Rejecting anime episode match due to season mismatch (expected S{season}, parsed {result_seasons}) for '{original_title}'")
                                 episode_match = False
                             elif not result_seasons:
                                 # Only use absolute episode validation when there's no season info at all
                                 has_absolute_episode = result.get('target_abs_episode') is not None
                                 if has_absolute_episode:
                                     episode_match = True
                                     logging.debug(f"Episode matched via XEM-mapped episode {episode} for '{original_title}' (with absolute episode validation)")
                                 else:
                                     logging.info(f"Rejecting anime episode match due to insufficient absolute episode evidence for S{season}E{episode}: '{original_title}'")
                                     episode_match = False
                             else:
                                 episode_match = False
                         else:
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
                                # re.search(rf'\b{original_abs}\b', original_title)):  # COMMENTED OUT: This regex causes false positives like matching "28" in "25 of 28"
                                False):  # Temporarily disabled regex fallback
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
                                    # re.search(rf'\b{abs_target}\b', original_title)):  # COMMENTED OUT: This regex causes false positives like matching numbers in episode counts
                                    False):  # Temporarily disabled regex fallback
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

            # --- END ADDED DEBUG LOGGING ---
            result['size'] = size_gb_for_filter 
            result['total_size_gb'] = total_size_gb
            
            # --- Bitrate Calculation (Consistent with Size Calculation) ---
            # Use the same per-item size that size filtering uses, with single-item runtime
            # This ensures consistency between size and bitrate calculations across all provider types
            
            bitrate = 0
            # runtime is the base runtime for one item (episode or movie) passed into filter_results
            
            if runtime is not None and runtime > 0 and size_gb_for_filter > 0:
                
                # Use per-item size with per-item runtime for consistent calculation
                bitrate = calculate_bitrate(size_gb_for_filter, runtime) 
            else:
                runtime_str = f"{runtime}min" if runtime is not None else "None"
                logging.warning(f"Skipping bitrate calculation for '{original_title}' due to non-positive runtime ({runtime_str}) or size_gb_for_filter ({size_gb_for_filter:.3f}GB). Bitrate set to 0.")
                bitrate = 0 
            
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
                # Only check content fields, exclude technical identifiers like binge_group
                original_fields_to_check = [original_title, filename]
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

            # Only check content fields, exclude technical identifiers like binge_group
            fields_to_check_patterns = [normalized_filter_title, normalized_filename]
            
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
            
            # --- F1 Movie Specific Filtering for tt16311594 ---
            if imdb_id == 'tt16311594':
                title_lower = original_title.lower()
                
                # Check for race-related terms that indicate this is not the movie
                race_indicators = [
                    'race', 'grand prix', 'round', 'fp', 'qualifying', 'sprint', 'testing',
                    'pre-race', 'post-race', 'pre-qualifying', 'post-qualifying', 'pre-sprint', 'post-sprint',
                    'weekend warm-up', 'drivers press conference', 'jolyon palmers analysis',
                    'australian grand prix', 'chinese grand prix', 'japanese grand prix', 'bahrain grand prix',
                    'saudi arabian grand prix', 'emilia romagna grand prix', 'monaco grand prix', 'spanish grand prix',
                    'british grand prix', 'belgian grand prix', 'hungarian grand prix', 'miami grand prix',
                    'austrian grand prix', 'dutch grand prix', 'italian grand prix', 'azerbaijan grand prix',
                    'singapore grand prix', 'united states grand prix', 'mexico grand prix', 'sao paulo grand prix',
                    'las vegas grand prix', 'qatar grand prix', 'abu dhabi grand prix'
                ]
                
                # Check if title contains any race indicators
                has_race_indicators = any(indicator in title_lower for indicator in race_indicators)
                
                # Check for movie-specific terms that indicate this IS the movie
                movie_indicators = [
                    'the movie', 'il film', 'la película', 'la pelicula', 'película', 'pelicula'
                ]
                
                # Check if title contains movie indicators
                has_movie_indicators = any(indicator in title_lower for indicator in movie_indicators)
                
                # Reject if it has race indicators and no movie indicators
                if has_race_indicators and not has_movie_indicators:
                    result['filter_reason'] = "F1 race content filtered out for movie search"
                    logging.info(f"Rejected: F1 race content detected for '{original_title}' (Size: {result['size']:.2f}GB)")
                    continue
                
                # Also reject if it has round numbers but no movie indicators
                if re.search(r'\br\d+\b', title_lower) and not has_movie_indicators:
                    result['filter_reason'] = "F1 round content filtered out for movie search"
                    logging.info(f"Rejected: F1 round content detected for '{original_title}' (Size: {result['size']:.2f}GB)")
                    continue
                
            # --- End F1 Movie Specific Filtering ---
            
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