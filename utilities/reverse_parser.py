import re
from utilities.settings import get_setting
import logging
import json

# Define regex metacharacters for detection at module level
REGEX_METACHARACTERS_FOR_DETECTION = r'.*?+^$()[]{}|\\'

def get_version_settings() -> dict:
    """
    Retrieves the version terms settings from the configuration.
    NOTE: If dynamic parsing from scraping versions is fully adopted,
    this might only be used for manual overrides or a different feature.
    """
    return get_setting('Reverse Parser', 'version_terms', {})

def get_default_version() -> str:
    """
    Retrieves the default version from the configuration for the Reverse Parser.
    This is used as a fallback in dynamic parsing if no scraping version matches.
    """
    return get_setting('Reverse Parser', 'default_version', 'unknown')

def get_version_order() -> list:
    """
    Retrieves the version order from the configuration.
    NOTE: Less relevant if dynamically matching against all scraping versions based on score.
    """
    return get_setting('Reverse Parser', 'version_order', [])

def parse_term(term, filename):
    """
    Parses a single term against the filename.
    Supports AND, OR logical operations and regex patterns.
    
    Args:
        term (str): The term to parse. Can be a simple string, or AND/OR expressions.
        filename (str): The filename to parse against.
    
    Returns:
        bool: True if the term condition is satisfied, False otherwise.
    """
    term = term.strip()
    
    # Handle AND/OR expressions
    if term.startswith('AND(') and term.endswith(')'):
        sub_terms = split_terms(term[4:-1])
        result = all(evaluate_sub_term(sub_term, filename) for sub_term in sub_terms)
        return result
    elif term.startswith('OR(') and term.endswith(')'):
        sub_terms = split_terms(term[3:-1])
        result = any(evaluate_sub_term(sub_term, filename) for sub_term in sub_terms)
        return result
    else:
        # Single term
        return evaluate_sub_term(term, filename)

def evaluate_sub_term(sub_term, filename):
    """
    Evaluates a sub-term against the filename.
    
    Args:
        sub_term (str): The sub-term to evaluate.
        filename (str): The filename to parse against.
    
    Returns:
        bool: True if the sub-term condition is satisfied, False otherwise.
    """
    sub_term = sub_term.strip()
    
    # Check if the term is a regex pattern
    if is_regex(sub_term):
        pattern = extract_regex(sub_term)
        try:
            match = re.search(pattern, filename, re.IGNORECASE)
            result = bool(match)
            return result
        except re.error as e:
            logging.error(f"Invalid regex pattern '{pattern}': {e}")
            return False
    else:
        # Simple substring match
        result = sub_term.lower() in filename.lower()
        return result

def is_regex(term: str) -> bool:
    """
    Determines if a term is a regex pattern based on delimiters.
    
    Args:
        term (str): The term to check.
    
    Returns:
        bool: True if term is a regex pattern, False otherwise.
    """
    return term.startswith('/') and term.endswith('/')

def extract_regex(term: str) -> str:
    """
    Extracts the regex pattern from a term.
    
    Args:
        term (str): The term containing the regex pattern.
    
    Returns:
        str: The extracted regex pattern.
    """
    return term[1:-1]

def split_terms(terms_str):
    """
    Splits a string of terms separated by commas, respecting parentheses and regex delimiters.
    
    Args:
        terms_str (str): The string containing multiple terms separated by commas.
    
    Returns:
        list: A list of individual terms.
    """
    terms = []
    current_term = ''
    in_regex = False
    escape = False
    
    for char in terms_str:
        if char == '/' and not escape:
            in_regex = not in_regex
        if char == ',' and not in_regex:
            terms.append(current_term.strip())
            current_term = ''
            continue
        if char == '\\' and not escape:
            escape = True
        else:
            escape = False
        current_term += char
    if current_term:
        terms.append(current_term.strip())
    return terms

def _is_likely_regex_pattern(pattern_str: str) -> bool:
    """
    Heuristically determines if a string is likely intended as a regex pattern
    by checking for common regex metacharacters.
    """
    if not pattern_str:
        return False
    # If any of the common regex metacharacters are present, assume it's a regex.
    # This aligns with how other parts of the application (e.g., smart_search) identify regex.
    return any(char in pattern_str for char in REGEX_METACHARACTERS_FOR_DETECTION)

def _compare_resolutions_from_ptt(ptt_resolution: str | None, version_max_res_str: str, version_wanted_op: str) -> bool:
    """Compares PTT-parsed resolution with version's resolution criteria."""
    if not ptt_resolution or ptt_resolution == 'Unknown':
        return False

    # PTT might return '720p', '1080p', '2160p'. Normalize if needed.
    # Ensure version_max_res_str is also normalized (e.g., "SD" from schema to "480p" or use PTT's values)
    # For simplicity, assuming PTT outputs match the common terms.
    # You might need a mapping if PTT uses different terms than your version_config
    
    res_order = {"480p": 1, "sd": 1, "720p": 2, "1080p": 3, "2160p": 4, "4k": 4, "uhd": 4}
    
    ptt_res_val = res_order.get(ptt_resolution.lower())
    
    normalized_version_max_res = version_max_res_str.lower()
    if normalized_version_max_res == "sd": # Schema uses SD
        normalized_version_max_res = "480p" # Map to a comparable PTT-like term
    version_max_res_val = res_order.get(normalized_version_max_res)

    if ptt_res_val is None or version_max_res_val is None:
        logging.debug(f"Cannot compare resolutions: PTT_res '{ptt_resolution}', version_max_res '{version_max_res_str}'")
        return False

    if version_wanted_op == "==":
        return ptt_res_val == version_max_res_val
    elif version_wanted_op == "<=":
        return ptt_res_val <= version_max_res_val
    elif version_wanted_op == ">=":
        return ptt_res_val >= version_max_res_val
    return False

def _term_matches(term: str, text_source: str | list | None) -> bool:
    """
    Checks if a term matches text.
    If text_source is a list, checks against each item, including items in nested lists.
    Case-insensitive for strings. Regex pattern matching is also case-insensitive.
    Detects regex based on content (presence of metacharacters), not slash delimiters.
    """
    if text_source is None:
        return False # No source to match against

    term_stripped = term.strip()
    if not term_stripped: # An empty term cannot match
        return False

    # Build a flat list of strings to check against
    sources_to_evaluate = []
    if isinstance(text_source, str):
        sources_to_evaluate.append(text_source)
    elif isinstance(text_source, list):
        for item in text_source:
            if item is None: # Skip None items in the list
                continue
            if isinstance(item, str):
                sources_to_evaluate.append(item)
            elif isinstance(item, list): # Handles nested lists
                for sub_item in item:
                    if sub_item is not None:
                        sources_to_evaluate.append(str(sub_item))
            else: # For other types in the list, convert to string
                sources_to_evaluate.append(str(item))
    
    if not sources_to_evaluate: # If after processing, there's nothing to check against
        return False

    # Determine if the term should be treated as a regex
    is_identified_as_regex = _is_likely_regex_pattern(term_stripped)
    compiled_pattern = None

    if is_identified_as_regex:
        try:
            compiled_pattern = re.compile(term_stripped, re.IGNORECASE)
        except re.error as e:
            logging.warning(f"Invalid regex pattern '{term_stripped}' in scoring: {e}. Will attempt literal string match instead.")
            is_identified_as_regex = False # Fallback to literal match

    for text in sources_to_evaluate:
        if is_identified_as_regex and compiled_pattern:
            if compiled_pattern.search(text):
                return True
        else: # Literal string match (either not identified as regex, or regex failed to compile)
            if term_stripped.lower() in text.lower():
                return True
    return False

# Helper function adapted from rank_results.py for use in _calculate_match_score
def _normalize_filter_pattern_rp(pattern: str) -> str:
    return re.sub(r'[\s-]+', '', pattern).lower()

# Helper function adapted from rank_results.py for use in _calculate_match_score
# Uses imported smart_search
def _check_preferred_rp(patterns_weights, fields_to_check: list, is_bonus: bool):
    from scraper.functions.other_functions import smart_search
    score_change = 0
    matched_normalized_patterns = set() 

    for item in patterns_weights:
        if not (isinstance(item, list) and len(item) == 2):
            logging.warning(f"Skipping malformed preferred_filter item: {item}")
            continue
        pattern, weight = item
        
        # Ensure pattern is a string for normalization and searching
        pattern_str = str(pattern)
        
        try:
            # Ensure weight can be converted to float
            weight_float = float(weight)
        except (ValueError, TypeError):
            logging.warning(f"Skipping preferred_filter item with invalid weight: {item}")
            continue

        normalized_pattern = _normalize_filter_pattern_rp(pattern_str)

        if normalized_pattern in matched_normalized_patterns:
            continue

        for field_value in fields_to_check:
            # smart_search expects string pattern and string text
            if smart_search(pattern_str, str(field_value)): 
                score_change += weight_float if is_bonus else -weight_float
                matched_normalized_patterns.add(normalized_pattern)
                break 
    return score_change #, breakdown

def _calculate_match_score(filename: str, ptt_data: dict, version_name: str, version_config: dict) -> float:
    """
    Calculates a score based on how well PTT-parsed data matches the scraping version_config,
    using logic adapted from filter_results.py and rank_results.py.
    Logs a single row detailing the outcome for this specific version.
    """
    from scraper.functions.other_functions import smart_search
    score = 0.0
    details_suffix = "" # To append details about filter_in bonus

    DEFAULT_HIGH_BONUS_FOR_FILTER_IN_MATCH = 2500.0 # Tunable: default high score if no preferred_filter_in scores exist
    SIGNIFICANT_RESOLUTION_MISMATCH_PENALTY = -150.0

    filename_display = (filename[:75] + '...') if len(filename) > 78 else filename
    version_name_display = (version_name[:17] + '...') if len(version_name) > 20 else version_name

    if ptt_data.get('parsing_error') or ptt_data.get('trash', False):
        details = "DQ: PTT error/trash"
        logging.debug(f"{filename_display:<80} | {version_name_display:<20} | {details:<45}")
        return -float('inf')

    combined_text_for_filter_out = (
        f"{filename} "
        f"{ptt_data.get('title','')} "
        f"{ptt_data.get('source','')} "
        f"{ptt_data.get('group','')}"
    )
    for term_obj in version_config.get("filter_out", []):
        term = str(term_obj) 
        if smart_search(term, combined_text_for_filter_out):
            term_display = (term[:15] + '...') if len(term) > 18 else term
            details = f"DQ: filter_out '{term_display}'"
            logging.debug(f"{filename_display:<80} | {version_name_display:<20} | {details:<45}")
            return -float('inf')

    ptt_matchable_fields_str = [
        str(field) for field in [
        ptt_data.get('source'),
        ptt_data.get('audio'),
        ptt_data.get('codec'),
        ptt_data.get('group'),
        filename
        ] if field is not None
    ]
    if not ptt_matchable_fields_str:
        ptt_matchable_fields_str = [filename]

    filter_in_terms = version_config.get("filter_in", [])
    if filter_in_terms: 
        found_mandatory_filter_in = False
        matched_filter_in_term_for_log = ""
        for term_obj in filter_in_terms:
            term = str(term_obj)
            if any(smart_search(term, field_val) for field_val in ptt_matchable_fields_str):
                found_mandatory_filter_in = True
                matched_filter_in_term_for_log = term # For logging
                break
        
        if not found_mandatory_filter_in:
            details = "DQ: Missing filter_in"
            logging.debug(f"{filename_display:<80} | {version_name_display:<20} | {details:<45}")
            return -float('inf')
        else:
            score += 20 # Base bonus for passing mandatory filter_in
            
            # Calculate special bonus based on highest preferred_filter_in score
            max_pref_in_score_for_version = 0.0
            has_preferred_filters = False
            preferred_filter_ins = version_config.get("preferred_filter_in", [])
            if preferred_filter_ins:
                has_preferred_filters = True
                for pref_item in preferred_filter_ins:
                    if isinstance(pref_item, list) and len(pref_item) == 2:
                        try:
                            weight = float(pref_item[1])
                            if weight > max_pref_in_score_for_version:
                                max_pref_in_score_for_version = weight
                        except (ValueError, TypeError):
                            continue # Skip malformed preferred_filter_in items
            
            bonus_from_filter_in_match = 0.0
            if max_pref_in_score_for_version > 0:
                bonus_from_filter_in_match = max_pref_in_score_for_version
            elif has_preferred_filters: # preferred_filter_in exists but all scores were <=0
                 bonus_from_filter_in_match = DEFAULT_HIGH_BONUS_FOR_FILTER_IN_MATCH # Still give default if non-positive max
            else: # No preferred_filter_in items at all
                bonus_from_filter_in_match = DEFAULT_HIGH_BONUS_FOR_FILTER_IN_MATCH
            
            score += bonus_from_filter_in_match
            term_display = (matched_filter_in_term_for_log[:10] + '...') if len(matched_filter_in_term_for_log) > 13 else matched_filter_in_term_for_log
            details_suffix += f" (FI-match:'{term_display}' +{bonus_from_filter_in_match:.0f})"

    # --- Stricter Resolution Matching ---
    ptt_resolution = ptt_data.get('resolution') # e.g., "1080p" from file
    version_max_res_str = version_config.get("max_resolution", "1080p") # e.g., "2160p" from version_config
    version_wanted_op = version_config.get("resolution_wanted", "==") # e.g., "==" from version_config

    resolution_criteria_met = _compare_resolutions_from_ptt(ptt_resolution, version_max_res_str, version_wanted_op)

    if version_wanted_op == "==":
        if not resolution_criteria_met:
            # Strict equality failed. Disqualify.
            actual_res_display = ptt_resolution if ptt_resolution else "None"
            details = f"DQ: Res mismatch (is {actual_res_display}, needs == {version_max_res_str})"
            logging.debug(f"{filename_display:<80} | {version_name_display:<20} | {details:<45}")
            return -float('inf')
        else:
            # Strict equality passed
            score += 50 
            score += float(version_config.get("resolution_weight", 0))
            # details_suffix += " (ResOk==)" 
    else: # Handles '<=' or '>='
        if resolution_criteria_met:
            # '<=' or '>=' condition met
            score += 50 
            score += float(version_config.get("resolution_weight", 0))
            # details_suffix += f" (ResOk{version_wanted_op})"
        elif ptt_resolution and ptt_resolution != 'Unknown': 
            # Directional condition NOT met, and file has a known resolution. Apply significant penalty.
            score += SIGNIFICANT_RESOLUTION_MISMATCH_PENALTY 
            actual_res_display = ptt_resolution if ptt_resolution else "None"
            details_suffix += f" (ResFail {actual_res_display}{version_wanted_op}{version_max_res_str} {SIGNIFICANT_RESOLUTION_MISMATCH_PENALTY:.0f})"
        # If ptt_resolution is None or Unknown, and it's not '==', no specific penalty here, relies on _compare_resolutions_from_ptt returning False.

    # --- HDR Match (logic remains the same) ---
    enable_hdr_for_version = version_config.get("enable_hdr", False)
    is_torrent_hdr = ptt_data.get('is_hdr', False)
    if not is_torrent_hdr:
        hdr_terms = ["hdr", "dv", "dolby vision", "hdr10", "hlg"]
        hdr_check_sources = [
            str(ptt_data.get('codec','')),
            str(ptt_data.get('source','')),
            filename.lower()
        ]
        hdr_check_text = " ".join(s for s in hdr_check_sources if s)
        if any(smart_search(hdr_term, hdr_check_text) for hdr_term in hdr_terms):
            is_torrent_hdr = True

    if is_torrent_hdr:
        if enable_hdr_for_version:
            score += 30
            score += float(version_config.get("hdr_weight", 0))
        else: 
            score -= 100 
    
    # --- Preferred Filters (logic remains the same) ---
    pref_out_score = _check_preferred_rp(
        version_config.get("preferred_filter_out", []),
        ptt_matchable_fields_str,
        is_bonus=False
    )
    score += pref_out_score

    pref_in_score = _check_preferred_rp(
        version_config.get("preferred_filter_in", []),
        ptt_matchable_fields_str,
        is_bonus=True
    )
    score += pref_in_score
    
    score_display = f"{score:.2f}"
    # Truncate details_suffix if it's too long before appending
    max_details_suffix_len = 43 - len(f"Score: {score_display} ") # Max length for suffix
    if len(details_suffix) > max_details_suffix_len:
        details_suffix = details_suffix[:max_details_suffix_len-3] + "..."

    details = f"Score: {score_display}{details_suffix}"
    if score == -float('inf'): # Should have already logged DQ and returned
        pass
    else:
        logging.debug(f"{filename_display:<80} | {version_name_display:<20} | {details:<45}")
    
    return score

def parse_filename_for_version(filename: str) -> str:
    """
    Dynamically parses a filename using PTT and then determines its best matching 
    "Scraping" version by scoring it against all configured scraping versions.
    Excludes generic version names '1080p' and '2160p' from being matched.
    """
    scraping_versions_config = get_setting('Scraping', 'versions', {})
    default_rp_version = get_default_version()
    
    excluded_generic_versions = []

    if not scraping_versions_config or not isinstance(scraping_versions_config, dict):
        logging.warning("No valid scraping versions configured or config is not a dict. Using Reverse Parser default.")
        return f"{default_rp_version}**"

    from scraper.functions.ptt_parser import parse_with_ptt
    ptt_data = parse_with_ptt(filename)
    
    filename_truncated_header = (filename[:75] + '...') if len(filename) > 78 else filename

    if ptt_data.get('parsing_error'):
        logging.warning(f"PTT parsing failed for '{filename_truncated_header}'. Using Reverse Parser default '{default_rp_version}'.")
        # Fall through to scoring, might still get a partial match or default correctly.
    
    if ptt_data.get('trash', False):
        logging.info(f"Filename '{filename_truncated_header}' marked as TRASH by PTT. Using RP default '{default_rp_version}'.")
        version_name_display = (default_rp_version[:17] + '...') if len(default_rp_version) > 20 else default_rp_version
        logging.debug(f"{filename_truncated_header:<80} | {version_name_display:<20} | {'DQ: PTT error/trash':<45}") # Increased details width
        return f"{default_rp_version}**"

    best_match_version_name = None
    highest_score = -float('inf')

    logging.debug(f"Scoring attempts for: {filename_truncated_header}")
    logging.debug(f"{'Processed Filename':<80} | {'Version Candidate':<20} | {'Details':<45}") # Increased details width
    logging.debug(f"{'-'*80} | {'-'*20} | {'-'*45}") # Increased details width

    for version_name, version_config in scraping_versions_config.items():
        if version_name in excluded_generic_versions:
            continue

        if not isinstance(version_config, dict):
            logging.warning(f"Skipping invalid version config for '{version_name}'. Expected dict, got {type(version_config)}")
            continue
        
        current_score = _calculate_match_score(filename, ptt_data, version_name, version_config)
        
        if current_score > highest_score:
            highest_score = current_score
            best_match_version_name = version_name

    MINIMUM_ACCEPTABLE_SCORE = 1.0 

    if best_match_version_name and highest_score >= MINIMUM_ACCEPTABLE_SCORE : 
        logging.info(f"Filename '{filename_truncated_header}' best matched scraping version '{best_match_version_name}' with score {highest_score:.2f}")
        return f"{best_match_version_name}*"
    else:
        logging.info(f"Filename '{filename_truncated_header}' did not sufficiently match any scraping version (best score: {highest_score:.2f}, min_req: {MINIMUM_ACCEPTABLE_SCORE}). Using RP default '{default_rp_version}'.")
        return f"{default_rp_version}**"

def parser_approximation(filename: str) -> dict:
    """
    Performs an approximation parse on the given filename.

    Args:
        filename (str): The filename to parse.

    Returns:
        dict: A dictionary containing parsed information, including the version.
    """
    # This function can be expanded later to include more parsing logic
    version = parse_filename_for_version(filename)
    return {
        'version': version,
        # Add other parsed information here as needed
    }