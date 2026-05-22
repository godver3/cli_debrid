import pickle
import os
import re
import logging
from utilities.settings import get_setting


def normalize_title(t: str) -> str:
    """Normalize a torrent title for not-wanted comparison.
    Lowercases, collapses dots to spaces, strips leading [group] tags and
    trailing container extensions so titles match regardless of formatting.
    """
    t = (t or '').lower()
    # Strip leading bracket tags like [tvN], [YIFY], [GroupName] — these vary
    # between indexers and cause mismatches on the same underlying torrent
    t = re.sub(r'^\s*\[[^\]]{1,20}\]\s*', '', t)
    t = re.sub(r'\.+', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    # Strip trailing container extensions so stored titles match Zilean titles
    t = re.sub(r'\s+(mkv|avi|mp4|mov|wmv|flv|webm|m4v|ts|m2ts|bdmv)$', '', t)
    return t

# Get db_content directory from environment variable with fallback
DB_CONTENT_DIR = os.environ.get('USER_DB_CONTENT', '/user/db_content')

# Update the paths to use the environment variable
NOT_WANTED_MAGNETS_FILE = os.path.join(DB_CONTENT_DIR, 'not_wanted_magnets.pkl')
NOT_WANTED_URLS_FILE = os.path.join(DB_CONTENT_DIR, 'not_wanted_urls.pkl')
NOT_WANTED_NZB_SEGMENTS_FILE = os.path.join(DB_CONTENT_DIR, 'not_wanted_nzb_segments.pkl')
NOT_WANTED_NZB_GUIDS_FILE = os.path.join(DB_CONTENT_DIR, 'not_wanted_nzb_guids.pkl')


def extract_nzb_segment_id(nzb_xml: str) -> str:
    """Extract the first segment Message-ID from NZB XML — identical across all indexers."""
    try:
        import xml.etree.ElementTree as ET
        # Strip namespace for easier parsing
        xml_clean = re.sub(r'\sxmlns="[^"]+"', '', nzb_xml, count=1)
        root = ET.fromstring(xml_clean)
        for file_el in root.iter('file'):
            segs = file_el.find('segments')
            if segs is not None:
                for seg in segs.iter('segment'):
                    msg_id = seg.text
                    if msg_id:
                        return msg_id.strip().strip('<>').lower()
    except Exception:
        pass
    return ''


def load_not_wanted_nzb_segments():
    try:
        with open(NOT_WANTED_NZB_SEGMENTS_FILE, 'rb') as f:
            return pickle.load(f)
    except (EOFError, pickle.UnpicklingError, FileNotFoundError):
        return set()


def save_not_wanted_nzb_segments(s):
    os.makedirs(os.path.dirname(NOT_WANTED_NZB_SEGMENTS_FILE), exist_ok=True)
    with open(NOT_WANTED_NZB_SEGMENTS_FILE, 'wb') as f:
        pickle.dump(s, f)


def add_to_not_wanted_nzb_segment(segment_id: str):
    if not segment_id:
        return
    s = load_not_wanted_nzb_segments()
    s.add(segment_id.strip().strip('<>').lower())
    save_not_wanted_nzb_segments(s)
    logging.info(f'[NZB] Added broken NZB segment ID {segment_id!r} to not-wanted list')


def is_nzb_segment_not_wanted(nzb_xml: str) -> bool:
    if get_setting('Debug', 'disable_not_wanted_check', False):
        return False
    seg_id = extract_nzb_segment_id(nzb_xml)
    if not seg_id:
        return False
    s = load_not_wanted_nzb_segments()
    if seg_id in s:
        logging.info(f'[NZB] Filtering out NZB — segment ID {seg_id!r} is in not-wanted list')
        return True
    return False


def extract_nzb_guid(url_or_guid: str) -> str:
    """Extract the indexer GUID from an NZB URL or guid string.
    Handles formats:
      - https://api.nzbgeek.info/api?t=get&id=ed914f26...
      - https://nzbgeek.info/geekseek.php?guid=ed914f26...
      - https://api.althub.co.za/getnzb/ed914f26...nzb
      - Plain guid string: ed914f26add1db0a7cc6a19c6358e5b0
    Returns normalized lowercase guid or empty string.
    """
    if not url_or_guid:
        return ''
    try:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(url_or_guid)
        qs = parse_qs(parsed.query)
        # ?id=... or ?guid=...
        for key in ('id', 'guid'):
            if key in qs:
                return qs[key][0].strip().lower()
        # Path-based: /getnzb/GUID.nzb or /getnzb/GUID&...
        path = parsed.path.rstrip('/')
        last = path.split('/')[-1]
        # Strip .nzb extension
        last = re.sub(r'\.nzb$', '', last, flags=re.IGNORECASE)
        # Strip query string remnants (althub appends &i=... to path)
        last = last.split('&')[0].split('?')[0]
        if last and re.match(r'^[0-9a-f]{16,}$', last, re.IGNORECASE):
            return last.lower()
    except Exception:
        pass
    # If it looks like a plain guid already
    if re.match(r'^[0-9a-f]{16,}$', url_or_guid.strip(), re.IGNORECASE):
        return url_or_guid.strip().lower()
    return ''


def load_not_wanted_nzb_guids():
    try:
        with open(NOT_WANTED_NZB_GUIDS_FILE, 'rb') as f:
            return pickle.load(f)
    except (EOFError, pickle.UnpicklingError, FileNotFoundError):
        return set()


def save_not_wanted_nzb_guids(s):
    os.makedirs(os.path.dirname(NOT_WANTED_NZB_GUIDS_FILE), exist_ok=True)
    with open(NOT_WANTED_NZB_GUIDS_FILE, 'wb') as f:
        pickle.dump(s, f)


def add_to_not_wanted_nzb_guid(url_or_guid: str):
    guid = extract_nzb_guid(url_or_guid)
    if not guid:
        return
    s = load_not_wanted_nzb_guids()
    if guid not in s:
        s.add(guid)
        save_not_wanted_nzb_guids(s)
        logging.info(f'[NZB] Added broken NZB guid {guid!r} to not-wanted list')


def is_nzb_guid_not_wanted(url_or_guid: str) -> bool:
    if get_setting('Debug', 'disable_not_wanted_check', False):
        return False
    guid = extract_nzb_guid(url_or_guid)
    if not guid:
        return False
    s = load_not_wanted_nzb_guids()
    if guid in s:
        logging.info(f'[NZB] Filtering out NZB — guid {guid!r} is in not-wanted list')
        return True
    return False


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
    if url is None:
        logging.debug("Received None value for URL/magnet in get_base_filename — skipping")
        return None

    if url.startswith('magnet:'):
        import re
        # Hex hash (40 chars, SHA1)
        btih_match = re.search(r'btih:([a-fA-F0-9]{40})(?:[&?]|$)', url, re.IGNORECASE)
        if btih_match:
            return btih_match.group(1).lower()
        # Base32 hash (32 chars, also valid btih encoding) — decode to hex for uniform comparison
        b32_match = re.search(r'btih:([A-Z2-7]{32})(?:[&?]|$)', url, re.IGNORECASE)
        if b32_match:
            try:
                import base64
                raw = base64.b32decode(b32_match.group(1).upper())
                return raw.hex().lower()
            except Exception:
                return b32_match.group(1).lower()
    
    # For URLs with file parameter
    if 'file=' in url:
        return url.split('file=')[-1].split('&')[0]
    
    # For direct URLs
    return url.split('/')[-1]

def is_magnet_not_wanted(magnet):
    if get_setting('Debug','disable_not_wanted_check', False):
        logging.debug(f"Not wanted check is disabled, allowing magnet: {magnet[:60] if magnet else 'None'}...")
        return False
        
    if magnet is None:
        logging.debug("Received None value for magnet in is_magnet_not_wanted — skipping check")
        return False
        
    not_wanted = load_not_wanted_magnets()
    
    # Extract hash from magnet link
    magnet_hash = get_base_filename(magnet)
    if magnet_hash is None:
        return False
        
    # Check if the hash exists in not_wanted
    is_not_wanted = magnet_hash in [get_base_filename(nw) for nw in not_wanted if nw is not None]
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

def validate_not_wanted_entries():
    """Validate the not wanted magnets and URLs files on boot."""
    logging.info("Validating not wanted entries...")
    
    # Validate magnets
    magnets = load_not_wanted_magnets()
    if magnets:
        logging.info(f"Found {len(magnets)} not wanted magnets")
        logging.info("First 5 magnet entries:")
        for i, magnet in enumerate(list(magnets)[:5]):
            if magnet is None:
                logging.error(f"Entry {i} is None - removing invalid entry")
                magnets.remove(None)
                continue
            logging.info(f"  {i+1}. {magnet[:60]}...")
    
    # Save cleaned magnets if any None values were removed
    if None in magnets:
        magnets.remove(None)
        save_not_wanted_magnets(magnets)
        logging.info("Cleaned up not wanted magnets list by removing None values")

if __name__ == '__main__':
    validate_not_wanted_entries()