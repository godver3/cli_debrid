import pickle
import os
import re
import logging
import threading
from utilities.atomic_io import atomic_write_bytes
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

# The membership checks run once per scrape result. Each list is cached in
# memory together with its normalized lookup keys, keyed by the file's stat
# signature, so a check neither re-reads the pickle nor re-normalizes every
# stored entry. Anything that rewrites or deletes a file changes its signature
# and forces a reload.
_store_lock = threading.RLock()
# (path, normalize function) -> (signature, frozenset of raw entries, frozenset of keys).
# Keyed by the normalizer too: a raw-entry read must not satisfy a later
# normalized lookup (a URL list's keys are basenames, not the stored URLs).
_store_cache = {}


def _file_signature(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def _write_pickled_set(path, values):
    with _store_lock:
        atomic_write_bytes(path, pickle.dumps(set(values)))
        for key in [k for k in _store_cache if k[0] == path]:
            del _store_cache[key]


def _read_pickled_set(path, create_if_missing):
    try:
        with open(path, 'rb') as f:
            return pickle.load(f)
    except (EOFError, pickle.UnpicklingError):
        # Empty or not a valid pickle
        return set()
    except FileNotFoundError:
        if create_if_missing:
            _write_pickled_set(path, set())
        return set()


def _cached_set(path, normalize=None, create_if_missing=False):
    """Return (raw entries, lookup keys) for a pickled set, reloading only on change."""
    with _store_lock:
        sig = _file_signature(path)
        entry = _store_cache.get((path, normalize))
        if entry is not None and sig is not None and entry[0] == sig:
            return entry[1], entry[2]
        raw = frozenset(_read_pickled_set(path, create_if_missing))
        if normalize is None:
            keys = raw
        else:
            keys = frozenset(k for k in (normalize(v) for v in raw if v is not None) if k is not None)
        # Cache under the signature taken before the read: if the file changed
        # mid-read, the next call sees a newer signature and reloads.
        if sig is not None:
            _store_cache[(path, normalize)] = (sig, raw, keys)
        return raw, keys


def _add_to_set(path, value, create_if_missing=False):
    """Add one entry under the lock. Returns False if it was already present."""
    with _store_lock:
        raw, _ = _cached_set(path, create_if_missing=create_if_missing)
        if value in raw:
            return False
        updated = set(raw)
        updated.add(value)
        _write_pickled_set(path, updated)
        return True


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


def add_to_not_wanted_nzb_segment(segment_id: str):
    if not segment_id:
        return
    _add_to_set(NOT_WANTED_NZB_SEGMENTS_FILE, segment_id.strip().strip('<>').lower())
    logging.info(f'[NZB] Added broken NZB segment ID {segment_id!r} to not-wanted list')


def is_nzb_segment_not_wanted(nzb_xml: str) -> bool:
    if get_setting('Debug', 'disable_not_wanted_check', False):
        return False
    seg_id = extract_nzb_segment_id(nzb_xml)
    if not seg_id:
        return False
    if seg_id in _cached_set(NOT_WANTED_NZB_SEGMENTS_FILE)[0]:
        logging.info(f'[NZB] Filtering out NZB — segment ID {seg_id!r} is in not-wanted list')
        return True
    return False


def extract_nzb_guid(url_or_guid: str) -> str:
    """Extract the indexer GUID from an NZB URL or guid string.
    Handles formats:
      - https://api.nzbgeek.info/api?t=get&id=ed914f26...
      - https://nzbgeek.info/geekseek.php?guid=ed914f26...
      - https://api.althub.co.za/getnzb/ed914f26...nzb
      - https://drunkenslug.com/getnzb/ed914f26....nzb&i=...&r=... (no '?'
        before the query params — some indexers' newznab feeds serve links
        like this; urlparse can't isolate a query string from it, so the
        whole "&i=...&r=..." tail ends up appended to the path segment
        instead)
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
        # Strip query string remnants (althub appends &i=... to path;
        # drunkenslug appends it with no leading '?' at all, which also
        # lands here since urlparse had nowhere else to put it)
        last = last.split('&')[0].split('?')[0]
        # Strip .nzb extension
        last = re.sub(r'\.nzb$', '', last, flags=re.IGNORECASE)
        if last and re.match(r'^[0-9a-f]{16,}$', last, re.IGNORECASE):
            return last.lower()
    except Exception:
        pass
    # If it looks like a plain guid already
    if re.match(r'^[0-9a-f]{16,}$', url_or_guid.strip(), re.IGNORECASE):
        return url_or_guid.strip().lower()
    return ''


def add_to_not_wanted_nzb_guid(url_or_guid: str):
    guid = extract_nzb_guid(url_or_guid)
    if not guid:
        return
    if _add_to_set(NOT_WANTED_NZB_GUIDS_FILE, guid):
        logging.info(f'[NZB] Added broken NZB guid {guid!r} to not-wanted list')


def is_nzb_guid_not_wanted(url_or_guid: str) -> bool:
    if get_setting('Debug', 'disable_not_wanted_check', False):
        return False
    guid = extract_nzb_guid(url_or_guid)
    if not guid:
        return False
    if guid in _cached_set(NOT_WANTED_NZB_GUIDS_FILE)[0]:
        logging.info(f'[NZB] Filtering out NZB — guid {guid!r} is in not-wanted list')
        return True
    return False


def load_not_wanted_magnets():
    return set(_cached_set(NOT_WANTED_MAGNETS_FILE, create_if_missing=True)[0])

def save_not_wanted_magnets(not_wanted_set):
    _write_pickled_set(NOT_WANTED_MAGNETS_FILE, not_wanted_set)

def add_to_not_wanted(hash_value, item_identifier=None, item=None):
    _add_to_set(NOT_WANTED_MAGNETS_FILE, hash_value, create_if_missing=True)

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
        
    # Extract hash from magnet link
    magnet_hash = get_base_filename(magnet)
    if magnet_hash is None:
        return False

    # Check if the hash exists in not_wanted
    _, not_wanted_keys = _cached_set(NOT_WANTED_MAGNETS_FILE, get_base_filename, create_if_missing=True)
    is_not_wanted = magnet_hash in not_wanted_keys
    if is_not_wanted:
        logging.info(f"Filtering out magnet {magnet[:60]}... as it is in not_wanted_magnets list")
    return is_not_wanted

def get_not_wanted_magnets():
    return load_not_wanted_magnets()

def get_not_wanted_urls():
    return load_not_wanted_urls()

def add_to_not_wanted_urls(url, item_identifier=None, item=None):
    _add_to_set(NOT_WANTED_URLS_FILE, url, create_if_missing=True)

def is_url_not_wanted(url):
    if get_setting('Debug','disable_not_wanted_check', False):
        logging.debug(f"Not wanted check is disabled, allowing URL: {url}")
        return False
    # Get base filename of the URL
    url_filename = get_base_filename(url)

    # Check if the filename exists in not_wanted
    _, not_wanted_keys = _cached_set(NOT_WANTED_URLS_FILE, get_base_filename, create_if_missing=True)
    is_not_wanted = url_filename in not_wanted_keys
    if is_not_wanted:
        logging.info(f"Filtering out URL {url} as it is in not_wanted_urls list")
    return is_not_wanted

def load_not_wanted_urls():
    return set(_cached_set(NOT_WANTED_URLS_FILE, create_if_missing=True)[0])

def save_not_wanted_urls(not_wanted_set):
    _write_pickled_set(NOT_WANTED_URLS_FILE, not_wanted_set)

def _purge_store(path, label):
    """Empty one not-wanted store. Returns how many entries it held."""
    with _store_lock:
        count = len(_cached_set(path, create_if_missing=True)[0])
        _write_pickled_set(path, set())
    logging.info(f"Purged {count} not wanted {label} ({os.path.basename(path)})")
    return count


def purge_not_wanted_magnets_file():
    return _purge_store(NOT_WANTED_MAGNETS_FILE, 'magnets')


def purge_not_wanted_urls_file():
    return _purge_store(NOT_WANTED_URLS_FILE, 'URLs')


def purge_not_wanted_nzbs_files():
    """NZBs are blocked by two stores: their article segment IDs and their indexer GUIDs."""
    return (_purge_store(NOT_WANTED_NZB_SEGMENTS_FILE, 'NZB segments')
            + _purge_store(NOT_WANTED_NZB_GUIDS_FILE, 'NZB GUIDs'))


def purge_all_not_wanted_files():
    """Empty every not-wanted store: magnets, URLs and NZBs. Returns {store: entries removed}."""
    return {
        'magnets': purge_not_wanted_magnets_file(),
        'urls': purge_not_wanted_urls_file(),
        'nzbs': purge_not_wanted_nzbs_files(),
    }

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