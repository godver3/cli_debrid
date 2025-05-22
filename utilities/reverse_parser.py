import re
from utilities.settings import get_setting
import logging
import json

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
    Checks if a term (string or /regex/) matches text.
    If text_source is a list, checks against each item.
    Case-insensitive for strings.
    """
    if text_source is None:
        return False
    
    term_stripped = term.strip()
    is_regex = term_stripped.startswith('/') and term_stripped.endswith('/') and len(term_stripped) > 1
    pattern = term_stripped[1:-1] if is_regex else None

    sources_to_check = []
    if isinstance(text_source, str):
        sources_to_check.append(text_source)
    elif isinstance(text_source, list):
        sources_to_check.extend([str(s) for s in text_source]) # Ensure all are strings

    for text in sources_to_check:
        if is_regex and pattern is not None:
            try:
                if bool(re.search(pattern, text, re.IGNORECASE)):
                    return True
            except re.error as e:
                logging.warning(f"Invalid regex '{pattern}' in scoring: {e}")
                return False # Or continue, depending on desired strictness
        elif not is_regex:
            if term_stripped.lower() in text.lower():
                return True
    return False

def _calculate_match_score(filename: str, ptt_data: dict, version_name: str, version_config: dict) -> float:
    """
    Calculates a score based on how well PTT-parsed data matches the scraping version_config.
    """
    score = 0.0
    
    if ptt_data.get('parsing_error') or ptt_data.get('trash', False): # If PTT marked as trash or error
        logging.debug(f"PTT parsing error or trash for '{filename}' for version '{version_name}'")
        return -float('inf') 

    # 1. Filter Out (critical: immediate disqualification)
    # Check against raw filename and key PTT fields like title, source, group
    combined_text_for_filter_out = f"{filename} {ptt_data.get('title','')} {ptt_data.get('source','')} {ptt_data.get('group','')}"
    for term in version_config.get("filter_out", []):
        if _term_matches(str(term), combined_text_for_filter_out): # Use the filename for raw matches
            logging.debug(f"'{filename}' DQ by filter_out '{term}' for version '{version_name}'")
            return -float('inf')

    # 2. Resolution Match (using PTT's parsed resolution)
    ptt_resolution = ptt_data.get('resolution')
    max_res = version_config.get("max_resolution", "1080p") # Default from schema
    res_wanted = version_config.get("resolution_wanted", "==")
    
    if _compare_resolutions_from_ptt(ptt_resolution, max_res, res_wanted):
        score += 50
        score += float(version_config.get("resolution_weight", 0))
    elif ptt_resolution and ptt_resolution != 'Unknown': # PTT found resolution, but it didn't match
        score -= 25

    # 3. HDR Match (PTT might have a specific field or it might be in 'other'/'codec')
    # Assuming PTT might put HDR info in 'codec' or as a general tag.
    # Your ptt_parser.py doesn't explicitly show an 'other' field, so we'll check common fields.
    enable_hdr = version_config.get("enable_hdr", False)
    if enable_hdr:
        hdr_terms = ["hdr", "dv", "dolby vision", "hdr10", "hlg"] # Check against PTT fields
        # Check codec, source, or even the original title if PTT doesn't isolate it well
        combined_hdr_check_text = f"{ptt_data.get('codec','')} {ptt_data.get('source','')} {filename.lower()}"
        if any(_term_matches(hdr_term, combined_hdr_check_text) for hdr_term in hdr_terms):
            score += 30
            score += float(version_config.get("hdr_weight", 0))

    # Data sources for term matching from PTT.
    # Your ptt_parser.py gives: 'source', 'audio', 'codec', 'group'.
    # It doesn't show an 'other' or 'tags' field, so we'll rely on these and the raw filename.
    ptt_matchable_fields = [
        ptt_data.get('source'),
        ptt_data.get('audio'),
        ptt_data.get('codec'),
        ptt_data.get('group'),
        filename # Always include raw filename as a fallback
    ]
    # Filter out None values
    ptt_matchable_fields = [field for field in ptt_matchable_fields if field is not None]


    # 4. Preferred Filter Out
    for pref_out_item in version_config.get("preferred_filter_out", []):
        if isinstance(pref_out_item, list) and len(pref_out_item) == 2:
            term, weight = pref_out_item
            if _term_matches(str(term), ptt_matchable_fields):
                score -= float(weight)

    # 5. Filter In
    for term in version_config.get("filter_in", []):
        if _term_matches(str(term), ptt_matchable_fields):
            score += 20

    # 6. Preferred Filter In
    for pref_in_item in version_config.get("preferred_filter_in", []):
        if isinstance(pref_in_item, list) and len(pref_in_item) == 2:
            term, weight = pref_in_item
            if _term_matches(str(term), ptt_matchable_fields):
                score += float(weight)
    
    logging.debug(f"Score for '{filename}' (PTT res: {ptt_resolution}) with version '{version_name}': {score}")
    return score

def parse_filename_for_version(filename: str) -> str:
    """
    Dynamically parses a filename using PTT and then determines its best matching 
    "Scraping" version by scoring it against all configured scraping versions.
    """
    scraping_versions_config = get_setting('Scraping', 'versions', {})
    default_rp_version = get_default_version()

    if not scraping_versions_config or not isinstance(scraping_versions_config, dict):
        logging.warning("No valid scraping versions configured. Using Reverse Parser default.")
        return f"{default_rp_version}**"

    from scraper.functions.ptt_parser import parse_with_ptt
    ptt_data = parse_with_ptt(filename) # Parse once
    logging.debug(f"--- Parsing filename: {filename} (PTT data: {ptt_data}) ---")

    if ptt_data.get('parsing_error'):
        logging.warning(f"PTT parsing failed for '{filename}'. Using Reverse Parser default.")
        # Consider if a PTT error should try to score anyway, or immediately default.
        # For now, we let it try to score, as _calculate_match_score handles parsing_error.
    
    # If PTT itself marks it as trash, we can directly assign default without scoring.
    if ptt_data.get('trash', False):
        logging.info(f"Filename '{filename}' marked as TRASH by PTT. Using RP default '{default_rp_version}'.")
        return f"{default_rp_version}**"


    best_match_version_name = None
    highest_score = -float('inf') # Initialize to a very low number

    for version_name, version_config in scraping_versions_config.items():
        if not isinstance(version_config, dict):
            logging.warning(f"Skipping invalid version config for '{version_name}'. Expected dict, got {type(version_config)}")
            continue
        
        current_score = _calculate_match_score(filename, ptt_data, version_name, version_config)
        
        if current_score > highest_score:
            highest_score = current_score
            best_match_version_name = version_name
        # Optional: Add tie-breaking logic here if needed

    # Adjust this threshold based on typical scores.
    # If PTT parsing fails or marks as trash, score will be -inf.
    MINIMUM_ACCEPTABLE_SCORE = 1.0 
    # This threshold is crucial. If resolution match is 50, a single preferred_filter_in might add 50 more.
    # A negative score from preferred_filter_out should ideally prevent a match.

    if best_match_version_name and highest_score >= MINIMUM_ACCEPTABLE_SCORE : # Use >= if 0 score is acceptable in some cases
        logging.info(f"Filename '{filename}' best matched scraping version '{best_match_version_name}' with score {highest_score:.2f}")
        return f"{best_match_version_name}*"
    else:
        logging.info(f"Filename '{filename}' did not sufficiently match any scraping version (best score: {highest_score:.2f}). Using RP default '{default_rp_version}'.")
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