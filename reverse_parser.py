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
    logging.debug(f"Parsing term: {term}")
    if term.startswith('AND(') and term.endswith(')'):
        sub_terms = term[4:-1].split(',')
        result = all(t.strip().lower() in filename.lower() for t in sub_terms)
        logging.debug(f"AND condition: {sub_terms}, result: {result}")
        return result
    elif term.startswith('OR(') and term.endswith(')'):
        sub_terms = term[3:-1].split(',')
        result = any(t.strip().lower() in filename.lower() for t in sub_terms)
        logging.debug(f"OR condition: {sub_terms}, result: {result}")
        return result
    else:
        result = term.strip().lower() in filename.lower()
        logging.debug(f"Simple term: {term}, result: {result}")
        return result

def parse_filename_for_version(filename):
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
            # Join all terms into a single string and then split by comma,
            # unless it's within parentheses
            joined_terms = ','.join(terms)
            term_list = []
            current_term = ''
            paren_count = 0
            for char in joined_terms:
                if char == '(' and current_term.startswith(('AND', 'OR')):
                    paren_count += 1
                elif char == ')':
                    paren_count -= 1
                
                if char == ',' and paren_count == 0:
                    term_list.append(current_term.strip())
                    current_term = ''
                else:
                    current_term += char
            
            if current_term:
                term_list.append(current_term.strip())
            
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