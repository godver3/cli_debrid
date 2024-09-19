import re
from settings import get_setting

def get_version_settings():
    return get_setting('Reverse Parser', 'version_terms', {})

def get_default_version():
    return get_setting('Reverse Parser', 'default_version', 'unknown')

def get_version_order():
    return get_setting('Reverse Parser', 'version_order', [])

def parse_filename_for_version(filename):
    version_settings = get_version_settings()
    default_version = get_default_version()
    version_order = get_version_order()

    # If version_order is not set, use the keys from version_settings
    if not version_order:
        version_order = list(version_settings.keys())

    for version in version_order:
        terms = version_settings.get(version, [])
        for term in terms:
            if re.search(rf'\b{re.escape(term)}\b', filename, re.IGNORECASE):
                return f"{version}*"  # Append asterisk to indicate local/approximate match

    return f"{default_version}*"

def parser_approximation(filename):
    # This function can be expanded later to include more parsing logic
    version = parse_filename_for_version(filename)
    return {
        'version': version,
        # Add other parsed information here as needed
    }