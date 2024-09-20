import re
from settings import get_setting
import logging
import json

def get_version_settings():
    return get_setting('Reverse Parser', 'version_terms', {})

def get_default_version():
    return get_setting('Reverse Parser', 'default_version', 'unknown')

def get_version_order():
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
    logging.debug(f"Parsing term: {term}")
    
    # Handle AND/OR expressions
    if term.startswith('AND(') and term.endswith(')'):
        sub_terms = split_terms(term[4:-1])
        result = all(evaluate_sub_term(sub_term, filename) for sub_term in sub_terms)
        logging.debug(f"AND condition: {sub_terms}, result: {result}")
        return result
    elif term.startswith('OR(') and term.endswith(')'):
        sub_terms = split_terms(term[3:-1])
        result = any(evaluate_sub_term(sub_term, filename) for sub_term in sub_terms)
        logging.debug(f"OR condition: {sub_terms}, result: {result}")
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
            logging.debug(f"Regex term: /{pattern}/, match: {match}, result: {result}")
            return result
        except re.error as e:
            logging.error(f"Invalid regex pattern '{pattern}': {e}")
            return False
    else:
        # Simple substring match
        result = sub_term.lower() in filename.lower()
        logging.debug(f"Simple term: {sub_term}, result: {result}")
        return result

def is_regex(term):
    """
    Determines if a term is a regex pattern based on delimiters.
    
    Args:
        term (str): The term to check.
    
    Returns:
        bool: True if term is a regex pattern, False otherwise.
    """
    return term.startswith('/') and term.endswith('/')

def extract_regex(term):
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

def parse_filename_for_version(filename):
    logging.info(f"Parsing filename: {filename}")
    version_settings = get_version_settings()
    default_version = get_default_version()
    version_order = get_version_order()

    logging.debug(f"Parsing filename: {filename}")
    logging.debug(f"Version settings: {json.dumps(version_settings, indent=2)}")

    # If version_order is not set, use the keys from version_settings
    if not version_order:
        version_order = list(version_settings.keys())

    for version in version_order:
        terms = version_settings.get(version, [])
        if terms:
            joined_terms = ','.join(terms)
            # Find all matches based on the regex pattern
            term_list = [match.strip() for match in re.findall(r'(AND\([^()]+\)|OR\([^()]+\)|[^,]+)', joined_terms)]

            logging.debug(f"Checking version {version} with terms: {term_list}")

            # Check if all terms match the filename
            if all(parse_term(term, filename) for term in term_list):
                logging.debug(f"Match found: Version {version}")
                return f"{version}*"  # One asterisk for local matches
            else:
                logging.debug(f"No match for version {version}")

    logging.debug(f"No match found, using default version: {default_version}")
    return f"{default_version}**"  # Two asterisks for default version

def parser_approximation(filename):
    # This function can be expanded later to include more parsing logic
    version = parse_filename_for_version(filename)
    return {
        'version': version,
        # Add other parsed information here as needed
    }