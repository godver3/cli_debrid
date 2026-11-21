"""Library section lookup, Plex-mode scan cooldown, scanning check and filename lookup."""

import asyncio
from utilities.settings import get_setting
import time
from typing import Dict, List, Any, Tuple, Optional
import plexapi.server
import os
from pathlib import Path
from plexapi.server import PlexServer
from plexapi.library import LibrarySection
import requests
from plexapi.exceptions import NotFound
import threading
from utilities import plex_health
from ._common import logger
from .collected import extract_show_ids, process_library_names
from .recent import process_recent_episode, process_recent_movie
from .scan_cleanup import plex_connection_settings
from utilities.paths import safe_realpath

def get_section_type(section: LibrarySection) -> Optional[str]:
    """Return the type of the Plex library section."""
    try:
        return section.type
    except Exception as e:
        logger.error(f"Error getting section type for section '{getattr(section, 'title', 'Unknown')}': {e}")
        return None

def find_plex_library_and_section(plex: PlexServer, item_path: str) -> Tuple[Optional[plexapi.library.Library], Optional[LibrarySection]]:
    """Find the Plex library and section containing the given item path."""
    try:
        # Ensure item_path is absolute and symlinks are resolved
        resolved_item_path = Path(safe_realpath(item_path))
        sections = plex.library.sections()
        # Keep track of the best match (longest common path)
        best_match_section = None
        max_common_len = -1

        for section in sections:
            # Ensure section.locations exists and is iterable
            if not hasattr(section, 'locations') or not section.locations:
                 logger.debug(f"Section '{section.title}' has no locations defined. Skipping.")
                 continue

            for location in section.locations:
                try:
                    # Ensure location is absolute and symlinks are resolved
                    resolved_location = Path(safe_realpath(location))

                    # Check if the item path is inside the location path
                    # Use Path.is_relative_to() for robust containment check
                    if resolved_item_path.is_relative_to(resolved_location):
                         # Calculate how much of the path matches (number of parts)
                         common_len = len(resolved_location.parts)
                         # If this is a more specific match (longer path) than previous ones, update
                         if common_len > max_common_len:
                             max_common_len = common_len
                             best_match_section = section
                             logger.debug(f"Potential match: Path '{item_path}' (resolved: {resolved_item_path}) is relative to location '{location}' (resolved: {resolved_location}) in section '{section.title}'")
                except ValueError as ve:
                    # is_relative_to raises ValueError if paths aren't comparable (e.g., different drives on Windows)
                    logger.debug(f"ValueError comparing paths: '{resolved_item_path}' and '{location}' (resolved: {resolved_location}). Skipping location. Error: {ve}")
                    continue
                except Exception as path_err:
                    logger.error(f"Error processing location '{location}' for section '{section.title}': {path_err}", exc_info=True)
                    continue # Skip this problematic location

        if best_match_section:
             logger.info(f"Found best match: Path '{item_path}' belongs to section '{best_match_section.title}'")
             return plex.library, best_match_section
        else:
            # This warning now correctly indicates no library root contained the item path
            logger.warning(f"Could not find Plex section containing path: {item_path} (Resolved: {resolved_item_path})")
            return None, None

    except NotFound:
         logger.error("Plex server connection issue: Library sections not found. Ensure Plex is running and connection details are correct.")
         return None, None
    except Exception as e:
        logger.error(f"Error finding Plex section for path '{item_path}': {e}", exc_info=True)
        return None, None

def _mirrored_library_path(directory: str, section_locations, max_levels: int = 3):
    """Find `directory` mirrored under a library location beside one of its ancestors.

    /data2/__all__/<release> with a library location /data2/shows returns
    /data2/shows/<release> when that path exists. Returns None otherwise.
    """
    locations = [os.path.abspath(os.path.normpath(loc)) for loc in section_locations if loc]
    ancestor = os.path.dirname(directory)
    for _ in range(max_levels):
        parent = os.path.dirname(ancestor)
        if not parent or parent == ancestor:
            break
        rest = os.path.relpath(directory, ancestor)
        for loc in locations:
            if loc == ancestor or os.path.dirname(loc) != parent:
                continue
            candidate = os.path.normpath(os.path.join(loc, rest))
            if os.path.exists(candidate):
                return candidate
        ancestor = parent
    return None

# Plex mode re-scans the same release folder from two places (the Checking queue
# and task_check_plex_files) every couple of minutes until Plex adds it: 4-6 scans
# of a folder whose contents never change, which piles up in Plex's scan queue
# when hundreds of releases finish an hour. Allow one scan per folder per cooldown;
# a later scan still happens if Plex missed the first. Symlink mode is left alone:
# season-pack episodes land in the same folder one at a time and need each scan.
PLEX_MODE_SCAN_COOLDOWN = 600

_plex_mode_scans: Dict[str, float] = {}

_plex_mode_scans_lock = threading.Lock()

def plex_mode_scan_due(path: str) -> bool:
    """True unless Plex mode already scanned ``path`` within the cooldown. Doesn't claim it."""
    if get_setting('File Management', 'file_collection_management') != 'Plex':
        return True
    with _plex_mode_scans_lock:
        last = _plex_mode_scans.get(os.path.normpath(path))
    return last is None or time.time() - last >= PLEX_MODE_SCAN_COOLDOWN

def claim_plex_mode_scan(path: str) -> bool:
    """True if ``path`` should be scanned now (and records it); False if it's on cooldown."""
    if get_setting('File Management', 'file_collection_management') != 'Plex':
        return True
    key = os.path.normpath(path)
    now = time.time()
    with _plex_mode_scans_lock:
        last = _plex_mode_scans.get(key)
        if last is not None and now - last < PLEX_MODE_SCAN_COOLDOWN:
            return False
        _plex_mode_scans[key] = now
        if len(_plex_mode_scans) > 5000:
            for k in [k for k, t in _plex_mode_scans.items() if now - t >= PLEX_MODE_SCAN_COOLDOWN]:
                del _plex_mode_scans[k]
    return True

# --- Asking Plex about one release (Plex mode's Checking timeout) -------------------
#
# Checked against a real Plex Media Server: ?file= matches a case-insensitive
# substring of the part path (and a show library needs type=4 or it answers 500), so
# matches are re-checked against the exact basename; while a library is scanned,
# /activities lists a 'library.update.section' activity.

PLEX_LOOKUP_TIMEOUT = 15

_SCANNING_CACHE_SECONDS = 30

_scanning_cache: Dict[str, Any] = {'at': 0.0, 'value': None}

_scanning_lock = threading.Lock()

def _plex_json(path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """GET a Plex endpoint as JSON. None (and a recorded failure) when Plex doesn't answer."""
    url, token = plex_connection_settings()
    if not url or not token:
        return None
    try:
        response = requests.get(f'{url}{path}', params=params or {}, timeout=PLEX_LOOKUP_TIMEOUT,
                                headers={'X-Plex-Token': token, 'Accept': 'application/json'})
        response.raise_for_status()
        return response.json().get('MediaContainer', {}) if response.content else {}
    except Exception as e:
        logger.warning(f"Plex didn't answer {path}: {e}")
        plex_health.record_contact_failure()
        return None

def plex_is_scanning() -> Optional[bool]:
    """True while Plex is scanning a library, False when idle, None if Plex didn't answer.

    Cached for 30 s: every held release in a Checking pass asks the same question.
    """
    now = time.time()
    with _scanning_lock:
        if _scanning_cache['value'] is not None and 0 <= now - _scanning_cache['at'] < _SCANNING_CACHE_SECONDS:
            return _scanning_cache['value']
    container = _plex_json('/activities')
    value = None
    if container is not None:
        value = any(str(a.get('type', '')).startswith('library.update.section')
                    for a in container.get('Activity', []) or [])
    with _scanning_lock:
        _scanning_cache.update(at=now, value=value)
    return value

def _plex_sections_for(item_type: str) -> Optional[List[Dict[str, Any]]]:
    """The configured Plex libraries that can hold ``item_type`` ('movie' or 'episode')."""
    container = _plex_json('/library/sections')
    if container is None:
        return None
    plex_type = 'show' if item_type == 'episode' else 'movie'
    sections = [d for d in container.get('Directory', []) or [] if d.get('type') == plex_type]
    setting = get_setting('Plex', 'shows_libraries' if plex_type == 'show' else 'movie_libraries', '')
    if setting:
        all_libraries = {d.get('title'): str(d.get('key')) for d in sections}
        by_key = {str(d.get('key')): d.get('title') for d in sections}
        allowed = set(process_library_names(setting, all_libraries, by_key))
        if allowed:
            sections = [d for d in sections if str(d.get('key')) in allowed]
    return sections

def find_plex_items_by_filename(filename: str, item_type: str,
                                _sections_cache: Optional[Dict[str, Any]] = None) -> Optional[List[Dict[str, Any]]]:
    """Plex metadata for the items with a part named exactly ``filename``.

    [] when Plex doesn't have it, None when Plex didn't answer. Pass the same dict
    as ``_sections_cache`` to look up several files with one library listing.
    """
    if not filename:
        return []
    if _sections_cache is not None and item_type in _sections_cache:
        sections = _sections_cache[item_type]
    else:
        sections = _plex_sections_for(item_type)
        if _sections_cache is not None and sections is not None:
            _sections_cache[item_type] = sections
    if sections is None:
        return None
    params = {'file': filename, 'includeGuids': 1, 'type': 4 if item_type == 'episode' else 1}
    found = []
    for section in sections:
        container = _plex_json(f"/library/sections/{section.get('key')}/all", params)
        if container is None:
            return None
        for meta in container.get('Metadata', []) or []:
            parts = [p.get('file') or '' for m in meta.get('Media', []) or [] for p in m.get('Part', []) or []]
            if any(os.path.basename(p) == filename for p in parts):
                found.append(meta)
    return found

def collect_from_plex(items: List[Dict[str, Any]]) -> Optional[Tuple[int, int]]:
    """Collect the Checking items whose file Plex already has, the way the recent scan would.

    Returns (files Plex has, items no longer in Checking afterwards), or None when
    Plex didn't answer. Used before giving up on a release: Plex can have added it
    since the last recently-added pass, and replacing it then deletes a file Plex
    is serving.
    """
    entries = []
    found_files = 0
    show_cache: Dict[str, Optional[Dict[str, Any]]] = {}
    sections_cache: Dict[str, Any] = {}
    for item in items:
        filename = os.path.basename(item.get('filled_by_file') or '')
        if not filename:
            continue
        metas = find_plex_items_by_filename(filename, item.get('type') or 'movie', sections_cache)
        if metas is None:
            return None
        if not metas:
            continue
        found_files += 1
        for meta in metas:
            if meta.get('type') == 'episode':
                show_key = str(meta.get('grandparentRatingKey') or '')
                if show_key not in show_cache:
                    container = _plex_json(f'/library/metadata/{show_key}', {'includeGuids': 1})
                    if container is None:
                        return None
                    shows = container.get('Metadata', []) or []
                    show_cache[show_key] = shows[0] if shows else None
                show = show_cache[show_key]
                if not show:
                    continue
                show_imdb, show_tmdb = extract_show_ids(show)
                entries += asyncio.run(process_recent_episode(
                    meta, show.get('title'), meta.get('parentIndex'), show_imdb, show_tmdb, show))
            else:
                entries += asyncio.run(process_recent_movie(meta))
    if not entries:
        return found_files, 0
    from database import add_collected_items
    from database.core import get_db_connection
    add_collected_items(entries, recent=True)
    ids = [item['id'] for item in items if item.get('id') is not None]
    if not ids:
        return found_files, 0
    conn = get_db_connection()
    try:
        still = conn.execute(
            f"SELECT COUNT(*) FROM media_items WHERE id IN ({','.join('?' * len(ids))}) AND state = 'Checking'",
            ids).fetchone()[0]
    finally:
        conn.close()
    return found_files, len(ids) - still
