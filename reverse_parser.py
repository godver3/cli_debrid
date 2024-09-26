import re
from settings import get_setting
import logging
import json

def get_version_settings() -> dict:
    """
    Retrieves the version terms settings from the configuration.

    Returns:
        dict: A dictionary containing version terms settings.
    """
    return get_setting('Reverse Parser', 'version_terms', {})

def get_default_version() -> str:
    """
    Retrieves the default version from the configuration.

    Returns:
        str: The default version string.
    """
    return get_setting('Reverse Parser', 'default_version', 'unknown')

def get_version_order() -> list:
    """
    Retrieves the version order from the configuration.

    Returns:
        list: A list representing the order of versions.
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

def parse_filename_for_version(filename: str) -> str:
    """
    Parses a filename to determine its version based on configured settings.

    Args:
        filename (str): The filename to parse.

    Returns:
        str: The determined version string, with asterisks indicating match type.
    """
    version_settings = get_version_settings()
    default_version = get_default_version()
    version_order = get_version_order()

    # If version_order is not set, use the keys from version_settings
    if not version_order:
        version_order = list(version_settings.keys())

    for version in version_order:
        terms = version_settings.get(version, [])
        if terms:
            joined_terms = ','.join(terms)
            # Find all matches based on the regex pattern
            term_list = [match.strip() for match in re.findall(r'(AND\([^()]+\)|OR\([^()]+\)|[^,]+)', joined_terms)]

            # Check if all terms match the filename
            if all(parse_term(term, filename) for term in term_list):
                return f"{version}*"  # One asterisk for local matches

    return f"{default_version}**"  # Two asterisks for default version

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