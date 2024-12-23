import pickle
import os
import logging
from settings import get_setting

# Get db_content directory from environment variable with fallback
DB_CONTENT_DIR = os.environ.get('USER_DB_CONTENT', '/user/db_content')

# Update the paths to use the environment variable
NOT_WANTED_MAGNETS_FILE = os.path.join(DB_CONTENT_DIR, 'not_wanted_magnets.pkl')
NOT_WANTED_URLS_FILE = os.path.join(DB_CONTENT_DIR, 'not_wanted_urls.pkl')

def load_not_wanted_magnets():
    try:
        with open(NOT_WANTED_MAGNETS_FILE, 'rb') as f:
            return pickle.load(f)
    except (EOFError, pickle.UnpicklingError):
        # If the file is empty or not a valid pickle object, return an empty set
        return set()
    except FileNotFoundError:
        # If the file does not exist, create it and return an empty set
        os.makedirs(os.path.dirname(NOT_WANTED_MAGNETS_FILE), exist_ok=True)
        with open(NOT_WANTED_MAGNETS_FILE, 'wb') as f:
            pickle.dump(set(), f)
        return set()

def save_not_wanted_magnets(not_wanted_set):
    os.makedirs(os.path.dirname(NOT_WANTED_MAGNETS_FILE), exist_ok=True)
    with open(NOT_WANTED_MAGNETS_FILE, 'wb') as f:
        pickle.dump(not_wanted_set, f)

def add_to_not_wanted(hash_value, item_identifier=None, item=None):
    not_wanted = load_not_wanted_magnets()
    not_wanted.add(hash_value)
    save_not_wanted_magnets(not_wanted)

def get_base_filename(url):
    """Extract the base filename from a URL or magnet link."""
    if url.startswith('magnet:'):
        # For magnet links, extract the hash
        import re
        btih_match = re.search(r'btih:([a-fA-F0-9]{40})', url)
        if btih_match:
            return btih_match.group(1).lower()
    
    # For URLs with file parameter
    if 'file=' in url:
        return url.split('file=')[-1].split('&')[0]
    
    # For direct URLs
    return url.split('/')[-1]

def is_magnet_not_wanted(magnet):
    if get_setting('Debug','disable_not_wanted_check', False):
        logging.debug(f"Not wanted check is disabled, allowing magnet: {magnet[:60]}...")
        return False
    not_wanted = load_not_wanted_magnets()
    
    # Extract hash from magnet link
    magnet_hash = get_base_filename(magnet)
    
    # Check if the hash exists in not_wanted
    is_not_wanted = magnet_hash in [get_base_filename(nw) for nw in not_wanted]
    if is_not_wanted:
        logging.info(f"Filtering out magnet {magnet[:60]}... as it is in not_wanted_magnets list")
    return is_not_wanted

def get_not_wanted_magnets():
    return load_not_wanted_magnets()

def get_not_wanted_urls():
    return load_not_wanted_urls()

def add_to_not_wanted_urls(url, item_identifier=None, item=None):
    not_wanted = load_not_wanted_urls()
    not_wanted.add(url)
    save_not_wanted_urls(not_wanted)

def is_url_not_wanted(url):
    if get_setting('Debug','disable_not_wanted_check', False):
        logging.debug(f"Not wanted check is disabled, allowing URL: {url}")
        return False
    not_wanted = load_not_wanted_urls()
    
    # Get base filename of the URL
    url_filename = get_base_filename(url)
    
    # Check if the filename exists in not_wanted
    is_not_wanted = url_filename in [get_base_filename(nw) for nw in not_wanted]
    if is_not_wanted:
        logging.info(f"Filtering out URL {url} as it is in not_wanted_urls list")
    return is_not_wanted

def load_not_wanted_urls():
    try:
        with open(NOT_WANTED_URLS_FILE, 'rb') as f:
            return pickle.load(f)
    except (EOFError, pickle.UnpicklingError):
        return set()
    except FileNotFoundError:
        os.makedirs(os.path.dirname(NOT_WANTED_URLS_FILE), exist_ok=True)
        with open(NOT_WANTED_URLS_FILE, 'wb') as f:
            pickle.dump(set(), f)
        return set()
    
def save_not_wanted_urls(not_wanted_set):
    os.makedirs(os.path.dirname(NOT_WANTED_URLS_FILE), exist_ok=True)
    with open(NOT_WANTED_URLS_FILE, 'wb') as f:
        pickle.dump(not_wanted_set, f)

def purge_not_wanted_magnets_file():
    # Purge the contents of the file by overwriting it with an empty set
    with open(NOT_WANTED_MAGNETS_FILE, 'wb') as f:
        pickle.dump(set(), f)
    print("The 'not_wanted_magnets.pkl' file has been purged.")