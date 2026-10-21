"""
Plex Collection Sync — mirrors content source list order into Plex collections.

Supports MDBList, Trakt Lists, and Adaptive List sources.
Collections are kept in sync with the source list: items added/removed/reordered
to match the desired sort order. Mixed lists (movies + shows) produce separate
collections with auto-suffixed names.

State is persisted to /user/config/plex_collection_state.json for efficient
incremental syncs — only changed items are processed on subsequent runs.
"""

import hashlib
import json
import logging
import os
import random as _random
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional

import requests

from database.core import get_db_connection
from utilities.settings import get_setting

logger = logging.getLogger(__name__)

# ── State file ────────────────────────────────────────────────────────────────
_STATE_FILE = os.path.join(os.environ.get('USER_CONFIG', '/user/config'), 'plex_collection_state.json')
_state_lock = threading.Lock()
# Serialises DB writes from concurrent collection-sync threads so SQLite's
# exclusive write lock is never contested by two threads simultaneously.
_db_write_lock = threading.Lock()

# ── In-process caches ─────────────────────────────────────────────────────────
_machine_id_cache: Optional[str] = None
_section_cache: dict = {}
_SECTION_CACHE_TTL = 300

# ── Show IMDb→ratingKey cache (avoids re-fetching 1000+ shows every sync) ────
_show_imdb_cache: dict = {}           # {(plex_url, section_key): {'data': {imdb: rk}, 'ts': float}}
_SHOW_CACHE_TTL = 600                 # 10 minutes

# ── Section items cache (IMDB/TMDB → ratingKey per section) ──────────────────
_section_items_cache: dict = {}       # {(plex_url, section_key): {'data': {'imdb:tt...': rk, 'tmdb:123': rk}, 'ts': float}}
_SECTION_ITEMS_CACHE_TTL = 120        # 2 minutes

# ── Concurrency guard ─────────────────────────────────────────────────────────
_sync_lock = threading.Lock()
_sync_in_flight: set = set()

# ── Trakt sort fields that require extra API call (Option C) ──────────────────
_TRAKT_OPTION_C_SORTS = {'listed_at', 'votes', 'rating', 'percentage', 'my_rating'}
# 'rank' uses default API order — no extra call needed


# ── State file helpers ────────────────────────────────────────────────────────

def _load_state() -> dict:
    """Load full state file. Returns {} on missing/corrupt."""
    with _state_lock:
        try:
            with open(_STATE_FILE, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}


def _save_state(state: dict) -> None:
    """Overwrite state file atomically."""
    with _state_lock:
        tmp = _STATE_FILE + '.tmp'
        try:
            with open(tmp, 'w') as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, _STATE_FILE)
        except Exception as e:
            logger.error(f"[PlexCollections] Failed to save state file: {e}")


def _get_source_state(source_id: str) -> dict:
    return _load_state().get(source_id, {})


def _update_source_state(source_id: str, updates: dict) -> None:
    """Merge updates into source_id's state entry."""
    state = _load_state()
    entry = state.get(source_id, {})
    entry.update(updates)
    state[source_id] = entry
    _save_state(state)


# ── Plex credentials ─────────────────────────────────────────────────────────

def _get_plex_credentials() -> tuple:
    plex_url = get_setting('Plex', 'url', '').rstrip('/')
    plex_token = get_setting('Plex', 'token', '')
    if not plex_url or not plex_token:
        raise ValueError("Plex URL or token not configured")
    return plex_url, plex_token


def _headers(token: str) -> dict:
    return {
        'X-Plex-Token': token,
        'Accept': 'application/json',
    }


# ── Machine ID ────────────────────────────────────────────────────────────────

def _get_machine_id(plex_url: str, token: str) -> str:
    global _machine_id_cache
    if _machine_id_cache:
        return _machine_id_cache
    resp = requests.get(f"{plex_url}/", headers={'X-Plex-Token': token, 'Accept': 'application/xml'}, timeout=30)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    mid = root.get('machineIdentifier', '')
    if not mid:
        raise ValueError("Could not retrieve Plex machineIdentifier")
    _machine_id_cache = mid
    return mid


# ── Library sections ──────────────────────────────────────────────────────────

def _get_library_sections(plex_url: str, token: str) -> dict:
    cache_key = (plex_url, token)
    now = time.time()
    cached = _section_cache.get(cache_key)
    if cached and (now - cached['ts']) < _SECTION_CACHE_TTL:
        return cached['data']
    resp = requests.get(f"{plex_url}/library/sections", headers=_headers(token), timeout=30)
    resp.raise_for_status()
    data = resp.json()
    sections = {}
    for d in data.get('MediaContainer', {}).get('Directory', []):
        key = str(d.get('key', ''))
        lib_type = d.get('type', '')
        title = d.get('title', '')
        if key and lib_type in ('movie', 'show'):
            sections[key] = {'type': lib_type, 'title': title}
    _section_cache[cache_key] = {'data': sections, 'ts': now}
    return sections


def _find_section_key(sections: dict, lib_type: str) -> Optional[str]:
    for key, info in sections.items():
        if info['type'] == lib_type:
            return key
    return None


def _get_section_id_map(plex_url: str, token: str, section_key: str, lib_type: str) -> dict:
    """
    Return {imdb_id: ratingKey, tmdb_id: ratingKey} for all items in a Plex section.
    Results are cached for _SECTION_ITEMS_CACHE_TTL seconds.
    Used to resolve section-local ratingKeys before adding items to a collection.
    """
    cache_key = (plex_url, section_key)
    now = time.time()
    cached = _section_items_cache.get(cache_key)
    if cached and (now - cached['ts']) < _SECTION_ITEMS_CACHE_TTL:
        return cached['data']

    plex_type = 1 if lib_type == 'movie' else 2
    try:
        resp = requests.get(
            f"{plex_url}/library/sections/{section_key}/all",
            headers=_headers(token),
            params={'type': plex_type, 'includeGuids': 1},
            timeout=60
        )
        resp.raise_for_status()
        id_map = {}
        for item in resp.json().get('MediaContainer', {}).get('Metadata', []):
            rk = str(item.get('ratingKey', ''))
            if not rk:
                continue
            for guid in item.get('Guid', []):
                gid = guid.get('id', '')
                if gid.startswith('imdb://'):
                    id_map[gid[7:]] = rk
                elif gid.startswith('tmdb://'):
                    id_map['tmdb:' + gid[7:]] = rk
        _section_items_cache[cache_key] = {'data': id_map, 'ts': now}
        return id_map
    except Exception as e:
        logger.warning(f"[PlexCollections] Failed to fetch section {section_key} items: {e}")
        return {}


# ── Collection CRUD ───────────────────────────────────────────────────────────

def _find_existing_collection(plex_url: str, token: str, section_key: str, name: str) -> Optional[str]:
    resp = requests.get(
        f"{plex_url}/library/sections/{section_key}/collections",
        headers=_headers(token), timeout=30
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    for item in data.get('MediaContainer', {}).get('Metadata', []):
        if item.get('title', '') == name:
            return str(item['ratingKey'])
    return None


# Sort options that map directly to Plex native collection sort modes (from plexapi source):
# collectionSort=0 = release date, collectionSort=1 = alpha/title, collectionSort=2 = custom/manual
# Endpoint: PUT /library/metadata/{ratingKey}/prefs?collectionSort=N
_PLEX_NATIVE_SORT_MAP = {
    'title':        1,
    'year':         0,
    'release_date': 0,
}

def _set_collection_sort_mode(plex_url: str, token: str, ratingkey: str, sort_mode: int) -> None:
    """Set Plex native collection sort (0=release date, 1=alpha, 2=custom)."""
    requests.put(
        f"{plex_url}/library/metadata/{ratingkey}/prefs?collectionSort={sort_mode}&X-Plex-Token={token}",
        headers={'Accept': 'application/json'}, timeout=30
    )


def _rename_collection(plex_url: str, token: str, section_key: str, ratingkey: str, new_title: str, sort_prefix: str = '!') -> None:
    """Rename a Plex collection if its current title differs."""
    try:
        r = requests.get(f"{plex_url}/library/collections/{ratingkey}",
                         headers=_headers(token), timeout=5)
        if r.status_code == 200:
            current_title = r.json().get('MediaContainer', {}).get('Metadata', [{}])[0].get('title', '')
            if current_title == new_title:
                return
        params = urllib.parse.urlencode({
            'type': 18, 'id': ratingkey,
            'title.value': new_title, 'title.locked': 1,
        })
        requests.put(f"{plex_url}/library/sections/{section_key}/all?{params}&X-Plex-Token={token}",
                     headers={'Accept': 'application/json'}, timeout=30)
        sort_title = f"{sort_prefix}{new_title}" if sort_prefix else new_title
        _set_collection_sort_title(plex_url, token, section_key, ratingkey, sort_title)
        logger.info(f"[PlexCollections] Renamed collection {ratingkey} to '{new_title}'")
    except Exception as e:
        logger.warning(f"[PlexCollections] Failed to rename collection {ratingkey}: {e}")


def _set_collection_sort_title(plex_url: str, token: str, section_key: str, ratingkey: str, sort_title: str) -> None:
    params = urllib.parse.urlencode({
        'type': 18,
        'id': ratingkey,
        'titleSort.value': sort_title,
        'titleSort.locked': 1,
    })
    url = f"{plex_url}/library/sections/{section_key}/all?{params}&X-Plex-Token={token}"
    resp = requests.put(url, headers={'Accept': 'application/json'}, timeout=30)
    if resp.status_code not in (200, 204):
        logger.warning(f"[PlexCollections] Set titleSort returned {resp.status_code} for collection {ratingkey}")


def _create_collection(plex_url: str, token: str, section_key: str, name: str, lib_type: str, sort_prefix: str = '!') -> str:
    plex_type = 1 if lib_type == 'movie' else 2
    params = {
        'type': plex_type,
        'title': name,
        'smart': 0,
        'sectionId': section_key,
    }
    url = f"{plex_url}/library/collections?{urllib.parse.urlencode(params)}&X-Plex-Token={token}"
    resp = requests.post(url, headers={'Accept': 'application/json'}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    rk = str(data['MediaContainer']['Metadata'][0]['ratingKey'])
    requests.put(
        f"{plex_url}/library/collections/{rk}/prefs?collectionSort=2&X-Plex-Token={token}",
        headers={'Accept': 'application/json'}, timeout=30
    )
    sort_title = f"{sort_prefix}{name}" if sort_prefix else name
    _set_collection_sort_title(plex_url, token, section_key, rk, sort_title)
    logger.info(f"[PlexCollections] Created collection '{name}' (sortTitle='{sort_title}', ratingKey={rk}) in section {section_key}")
    return rk


def _get_or_create_collection(plex_url: str, token: str, section_key: str, name: str, lib_type: str, sort_prefix: str = '!') -> str:
    rk = _find_existing_collection(plex_url, token, section_key, name)
    if rk:
        return rk
    return _create_collection(plex_url, token, section_key, name, lib_type, sort_prefix)


# ── Collection items ──────────────────────────────────────────────────────────

def _get_collection_items(plex_url: str, token: str, ratingkey: str) -> list:
    resp = requests.get(
        f"{plex_url}/library/collections/{ratingkey}/children",
        headers=_headers(token), timeout=120
    )
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    data = resp.json()
    return [str(m['ratingKey']) for m in data.get('MediaContainer', {}).get('Metadata', [])]


def _add_items_to_collection(plex_url: str, token: str, machine_id: str, ratingkey: str, item_ratingkeys: list) -> None:
    total = len(item_ratingkeys)
    failed_batches = 0
    for i in range(0, total, 100):
        batch = item_ratingkeys[i:i + 100]
        csv_keys = ','.join(batch)
        uri = f"server://{machine_id}/com.plexapp.plugins.library/library/metadata/{csv_keys}"
        url = f"{plex_url}/library/collections/{ratingkey}/items?uri={urllib.parse.quote(uri)}&X-Plex-Token={token}"
        resp = requests.put(url, headers={'Accept': 'application/json'}, timeout=60)
        if resp.status_code not in (200, 201):
            logger.warning(f"[PlexCollections] Add items batch {i//100+1} returned {resp.status_code} for collection {ratingkey} (batch keys: {batch[:3]}...)")
            failed_batches += 1
    logger.info(f"[PlexCollections] _add_items_to_collection: {total} items in {(total+99)//100} batches, {failed_batches} failed batches")


def _remove_item_from_collection(plex_url: str, token: str, ratingkey: str, item_ratingkey: str) -> None:
    url = f"{plex_url}/library/collections/{ratingkey}/items/{item_ratingkey}?X-Plex-Token={token}"
    resp = requests.delete(url, headers={'Accept': 'application/json'}, timeout=30)
    if resp.status_code not in (200, 204):
        logger.warning(f"[PlexCollections] Remove item {item_ratingkey} returned {resp.status_code}")


# ── Ordering ──────────────────────────────────────────────────────────────────

def _arrange_items_in_order(plex_url: str, token: str, ratingkey: str, desired_order: list) -> None:
    """
    Move items into desired_order using LCS-based minimal-move algorithm.
    Only moves items that are out of position.
    """
    current_order = _get_collection_items(plex_url, token, ratingkey)
    if current_order == desired_order:
        logger.debug(f"[PlexCollections] Collection {ratingkey} already in correct order, skipping moves")
        return

    desired_set = set(desired_order)
    current_filtered = [k for k in current_order if k in desired_set]

    # LCS via patience sort — O(n log n)
    current_pos = {k: i for i, k in enumerate(current_filtered)}
    tails = []
    indices = []

    for d_idx, key in enumerate(desired_order):
        if key not in current_pos:
            continue
        c_idx = current_pos[key]
        lo, hi = 0, len(tails)
        while lo < hi:
            mid = (lo + hi) // 2
            if tails[mid] < c_idx:
                lo = mid + 1
            else:
                hi = mid
        tails[lo:lo+1] = [c_idx]
        indices.append((lo, key, d_idx))

    stay = set()
    if tails:
        need = len(tails) - 1
        for pos, key, d_idx in reversed(indices):
            if pos == need:
                stay.add(key)
                need -= 1
                if need < 0:
                    break

    moves = 0
    for i, item_key in enumerate(desired_order):
        if item_key in stay:
            continue
        if item_key not in current_pos and item_key not in set(current_order):
            continue

        predecessor = None
        for j in range(i - 1, -1, -1):
            if desired_order[j] in current_pos or desired_order[j] in set(current_order):
                predecessor = desired_order[j]
                break

        if predecessor is None:
            url = f"{plex_url}/library/collections/{ratingkey}/items/{item_key}/move?X-Plex-Token={token}"
        else:
            url = f"{plex_url}/library/collections/{ratingkey}/items/{item_key}/move?after={predecessor}&X-Plex-Token={token}"

        for _attempt in range(3):
            try:
                resp = requests.put(url, headers={'Accept': 'application/json'}, timeout=30)
                if resp.status_code not in (200, 204):
                    logger.warning(f"[PlexCollections] Move item {item_key} returned {resp.status_code}")
                else:
                    moves += 1
                break
            except requests.exceptions.Timeout:
                if _attempt < 2:
                    time.sleep(2)
                else:
                    logger.warning(f"[PlexCollections] Move item {item_key} timed out after 3 attempts, skipping")
        time.sleep(0.05)

    logger.info(f"[PlexCollections] Reorder complete: {moves} moves for collection {ratingkey} ({len(desired_order)} items)")


# ── Sort engine ───────────────────────────────────────────────────────────────

def _apply_sort(items: list, sort_option: str, sort_how: str, trakt_metadata: dict = None) -> list:
    """
    Sort collected items by the given option.
    'default' returns items unchanged (preserves live source order).
    DB-based sorts use fields already in media_items.
    Trakt Option C sorts use trakt_metadata dict {imdb_id: {field: value}}.
    Items missing the sort key sort last (for asc) or first (for desc).
    """
    if sort_option == 'default':
        return list(items)

    if sort_option == 'random':
        items_copy = list(items)
        _random.shuffle(items_copy)
        return items_copy

    reverse = (sort_how == 'desc')
    INF = float('inf')

    def sort_key(item):
        if sort_option == 'title':
            t = (item.get('title') or '').strip()
            # Strip leading articles to match Plex sort convention
            for article in ('The ', 'A ', 'An '):
                if t.startswith(article):
                    t = t[len(article):]
                    break
            return t.lower()
        elif sort_option == 'year':
            v = item.get('year')
            return v if v is not None else (INF if not reverse else -INF)
        elif sort_option == 'release_date':
            return item.get('release_date') or ''
        elif sort_option == 'collected_at':
            return item.get('collected_at') or ''
        elif sort_option == 'runtime':
            v = item.get('runtime')
            return v if v is not None else (INF if not reverse else -INF)
        elif sort_option == 'rank' and trakt_metadata:
            imdb = item.get('imdb_id', '')
            v = (trakt_metadata.get(imdb) or {}).get('rank')
            return v if v is not None else (INF if not reverse else -INF)
        elif trakt_metadata and sort_option in _TRAKT_OPTION_C_SORTS:
            imdb = item.get('imdb_id', '')
            v = (trakt_metadata.get(imdb) or {}).get(sort_option)
            return v if v is not None else (INF if not reverse else -INF)
        return ''

    return sorted(items, key=sort_key, reverse=reverse)


# ── Trakt metadata fetchers ───────────────────────────────────────────────────

def _fetch_trakt_list_updated_at(trakt_lists_url: str) -> Optional[str]:
    """
    Lightweight GET /users/{user}/lists/{id} to check updated_at.
    Returns ISO timestamp string or None on failure.
    """
    try:
        from content_checkers.trakt import (
            get_trakt_headers, parse_trakt_list_url,
            clean_username_for_api, TRAKT_API_URL
        )
        list_info = parse_trakt_list_url(trakt_lists_url)
        if not list_info:
            return None
        username = clean_username_for_api(list_info['username'])
        list_id = list_info['list_id']
        headers = get_trakt_headers()
        resp = requests.get(
            f"{TRAKT_API_URL}/users/{username}/lists/{list_id}",
            headers=headers, timeout=30
        )
        if resp.status_code == 200:
            return resp.json().get('updated_at')
    except Exception as e:
        logger.warning(f"[PlexCollections] Trakt updated_at check failed: {e}")
    return None


def _fetch_trakt_list_sorted(trakt_lists_url: str, sort_by: str, sort_how: str, media_type: str = 'All') -> list:
    """
    Fetch Trakt list items sorted server-side via URL path params.
    Trakt API: /users/{id}/lists/{list_id}/items/{type}/{sort_by}/{sort_how}
    Returns ordered list of (imdb_id, tmdb_id) pairs.
    VIP-only sorts fall back to rank for non-VIP accounts (handled by Trakt).
    """
    try:
        from content_checkers.trakt import (
            get_trakt_headers, parse_trakt_list_url,
            clean_username_for_api, TRAKT_API_URL, PAGINATION_LIMIT
        )
        list_info = parse_trakt_list_url(trakt_lists_url)
        if not list_info:
            return []
        username = clean_username_for_api(list_info['username'])
        list_id = list_info['list_id']
        headers = get_trakt_headers()
        all_items = []
        page = 1
        page_count = 1

        # Map content source media_type to Trakt type segment
        # 'Movies' -> 'movies', 'Shows' -> 'shows'
        # 'All' -> fetch movies and shows separately then combine
        # (omitting type segment causes Trakt to misinterpret sort_by as the type)
        if media_type == 'Shows':
            type_segments = ['shows/']
        elif media_type == 'All':
            type_segments = ['movies/', 'shows/']
        else:
            type_segments = ['movies/']

        all_items_combined = []
        for type_segment in type_segments:
            page = 1
            page_count = 1
            while page <= page_count:
                # Trakt API sort path: /users/{id}/lists/{id}/items/{type}/{sort_by}/{sort_how}
                url = (f"{TRAKT_API_URL}/users/{username}/lists/{list_id}/items"
                       f"/{type_segment}{sort_by}/{sort_how}?limit={PAGINATION_LIMIT}&page={page}")
                resp = None
                for _attempt in range(4):
                    resp = requests.get(url, headers=headers, timeout=30)
                    if resp.status_code == 429:
                        retry_after = int(resp.headers.get('Retry-After', 10))
                        logger.warning(f"[PlexCollections] Trakt rate limited (429) on {type_segment} page {page}, waiting {retry_after}s (attempt {_attempt+1}/4)")
                        time.sleep(retry_after)
                    else:
                        break
                if resp.status_code != 200:
                    logger.warning(f"[PlexCollections] Trakt sorted fetch {type_segment} returned {resp.status_code}")
                    break
                items = resp.json()
                if page == 1:
                    page_count = int(resp.headers.get('X-Pagination-Page-Count', 1))
                    applied_sort = resp.headers.get('X-Applied-Sort-By', sort_by)
                    logger.info(f"[PlexCollections] Trakt sort applied: {applied_sort}/{sort_how} type={type_segment} ({page_count} pages)")
                for entry in items:
                    media = entry.get('movie') or entry.get('show') or {}
                    ids = media.get('ids', {})
                    imdb = ids.get('imdb')
                    tmdb = ids.get('tmdb')
                    if imdb or tmdb:
                        all_items_combined.append((imdb, str(tmdb) if tmdb else None))
                if page < page_count:
                    time.sleep(0.5)
                page += 1

        first_3 = [i[0] for i in all_items_combined[:3]]
        logger.info(f"[PlexCollections] Fetched {len(all_items_combined)} sorted Trakt items (sort={sort_by}/{sort_how}, types={type_segments}, first 3 imdb: {first_3})")
        return all_items_combined
    except Exception as e:
        logger.error(f"[PlexCollections] Trakt sorted fetch failed: {e}", exc_info=True)
        return []


# ── Fingerprint ───────────────────────────────────────────────────────────────

def _compute_fingerprint(source_id: str, ordered_imdb_ids: list = None) -> str:
    """Hash of ordered imdb_ids. Uses live list when provided, else DB fallback."""
    if ordered_imdb_ids is not None:
        return hashlib.md5(','.join(ordered_imdb_ids).encode()).hexdigest()
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """SELECT imdb_id FROM media_items
               WHERE content_source = ? AND state = 'Collected' AND imdb_id IS NOT NULL
               ORDER BY source_position ASC NULLS LAST, id ASC""",
            (source_id,)
        ).fetchall()
    finally:
        conn.close()
    imdb_ids = [r['imdb_id'] for r in rows if r['imdb_id']]
    return hashlib.md5(','.join(imdb_ids).encode()).hexdigest()


# ── DB sync state ─────────────────────────────────────────────────────────────

def _get_sync_state(source_id: str) -> dict:
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT * FROM plex_collection_sync WHERE source_id = ?", (source_id,)
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _save_sync_state(source_id: str, movie_rk: Optional[str], show_rk: Optional[str], fingerprint: str, sort_option: str = 'default') -> None:
    with _db_write_lock:
        conn = get_db_connection()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO plex_collection_sync
                   (source_id, movie_collection_ratingkey, show_collection_ratingkey, last_fingerprint, last_synced_at, sort_option)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (source_id, movie_rk, show_rk, fingerprint,
                 datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                 sort_option)
            )
            conn.commit()
        finally:
            conn.close()


def _get_ratingkey_for_section(source_id: str, section_key: str, lib_type: str) -> Optional[str]:
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT ratingkey FROM plex_collection_sync_libraries WHERE source_id=? AND section_key=? AND lib_type=?",
            (source_id, section_key, lib_type)
        ).fetchone()
        return row['ratingkey'] if row else None
    finally:
        conn.close()


def _save_ratingkey_for_section(source_id: str, section_key: str, lib_type: str, ratingkey: Optional[str]) -> None:
    with _db_write_lock:
        conn = get_db_connection()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO plex_collection_sync_libraries (source_id, section_key, lib_type, ratingkey) VALUES (?,?,?,?)",
                (source_id, section_key, lib_type, ratingkey)
            )
            conn.commit()
        finally:
            conn.close()


# ── Main sync ─────────────────────────────────────────────────────────────────

def sync_collection_for_source(source_id: str, source_data: dict, ordered_imdb_ids: list = None, ordered_pairs: list = None) -> None:
    """
    Full sync: diff current Plex collection against desired state and reconcile.
    Uses state file for efficient incremental updates.
    """
    coll_cfg = source_data.get('plex_collection', {})
    if not isinstance(coll_cfg, dict):
        coll_cfg = {}
    if not coll_cfg.get('enabled', False):
        return

    sort_option = coll_cfg.get('sort_by', 'default')
    sort_how = coll_cfg.get('sort_how', 'asc')
    source_type = source_data.get('type', '')
    trakt_lists_url = source_data.get('trakt_lists', '') if source_type == 'Trakt Lists' else ''

    try:
        plex_url, plex_token = _get_plex_credentials()
        machine_id = _get_machine_id(plex_url, plex_token)
        sections = _get_library_sections(plex_url, plex_token)
    except Exception as e:
        logger.error(f"[PlexCollections] Cannot connect to Plex for {source_id}: {e}")
        return

    # Resolve target section keys — use user selection if configured, else fall back to first-of-type
    plex_coll_cfg = source_data.get('plex_collection', {})
    if not isinstance(plex_coll_cfg, dict):
        plex_coll_cfg = {}
    raw_lib_selection = plex_coll_cfg.get('libraries', [])
    if isinstance(raw_lib_selection, str):
        import json as _json
        try:
            raw_lib_selection = _json.loads(raw_lib_selection)
        except Exception:
            raw_lib_selection = []
    if raw_lib_selection and isinstance(raw_lib_selection, list):
        selected_keys = [str(k) for k in raw_lib_selection if str(k) in sections]
    else:
        selected_keys = []
    # Fall back: if nothing selected, use first of each type (existing behaviour)
    if not selected_keys:
        fallback_movie = _find_section_key(sections, 'movie')
        fallback_show = _find_section_key(sections, 'show')
        selected_keys = [k for k in [fallback_movie, fallback_show] if k]
    movie_section_keys = [k for k in selected_keys if sections[k]['type'] == 'movie']
    show_section_keys  = [k for k in selected_keys if sections[k]['type'] == 'show']

    # ── Query ALL Collected items for matching ────────────────────────────────
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """SELECT DISTINCT imdb_id, tmdb_id, ms_item_id, type,
                      title, year, release_date, collected_at, runtime,
                      MAX(collected_at) as collected_at
               FROM media_items
               WHERE state = 'Collected' AND ms_item_id IS NOT NULL
               GROUP BY imdb_id, tmdb_id, ms_item_id, type""",
        ).fetchall()
    finally:
        conn.close()

    by_imdb = {}
    by_tmdb = {}
    tmdb_map = {}  # {ms_item_id: (tmdb_id, type)} for poster thumb fallback
    for r in rows:
        imdb = r['imdb_id']
        tmdb = str(r['tmdb_id']) if r['tmdb_id'] else None
        ms = r['ms_item_id']
        d = dict(r)
        if imdb and imdb not in by_imdb:
            by_imdb[imdb] = d
        if tmdb and tmdb not in by_tmdb:
            by_tmdb[tmdb] = d
        if ms and tmdb:
            tmdb_map[ms] = (tmdb, r['type'] or 'movie')

    # ── Determine which items belong in this collection ───────────────────────
    if ordered_pairs:
        items_all = []
        seen_ms = set()
        matched = 0
        for imdb, tmdb in ordered_pairs:
            item = by_imdb.get(imdb) or by_tmdb.get(str(tmdb) if tmdb else '')
            if item:
                ms = item.get('ms_item_id')
                if ms and ms not in seen_ms:
                    seen_ms.add(ms)
                    items_all.append(item)
                    matched += 1
        logger.info(f"[PlexCollections] {source_id}: {matched}/{len(ordered_pairs)} ordered_pairs matched collected items (by_imdb={len(by_imdb)}, by_tmdb={len(by_tmdb)})")
    elif ordered_imdb_ids:
        items_all = [by_imdb[imdb] for imdb in ordered_imdb_ids if imdb in by_imdb]
    else:
        # No ordering info provided — query items for this specific source from DB
        conn2 = get_db_connection()
        try:
            source_rows = conn2.execute(
                """SELECT DISTINCT imdb_id, tmdb_id, ms_item_id, type,
                          title, year, release_date, collected_at, runtime,
                          MAX(collected_at) as collected_at
                   FROM media_items
                   WHERE state = 'Collected' AND ms_item_id IS NOT NULL
                     AND content_source = ?
                   GROUP BY imdb_id, tmdb_id, ms_item_id, type
                   ORDER BY collected_at DESC""",
                (source_id,)
            ).fetchall()
        finally:
            conn2.close()
        items_all = []
        seen_ms2 = set()
        for r in source_rows:
            ms = r['ms_item_id']
            if ms and ms not in seen_ms2:
                seen_ms2.add(ms)
                items_all.append(dict(r))

    # ── For Trakt: re-fetch with server-side sort when sort ≠ default ─────────
    # Trakt API supports sort_by/sort_how as URL path params, handled server-side.
    # This gives correct order without client-side metadata fetching.
    if source_type == 'Trakt Lists' and trakt_lists_url and sort_option not in ('default', ''):
        source_media_type = source_data.get('media_type', 'All')
        sorted_pairs = _fetch_trakt_list_sorted(trakt_lists_url, sort_option, sort_how, source_media_type)
        if sorted_pairs:
            # Re-build items_all in the sorted order returned by Trakt
            items_all = []
            seen_ms = set()
            for imdb, tmdb in sorted_pairs:
                item = by_imdb.get(imdb) or by_tmdb.get(str(tmdb) if tmdb else '')
                if item:
                    ms = item.get('ms_item_id')
                    if ms and ms not in seen_ms:
                        seen_ms.add(ms)
                        items_all.append(item)

    # ── Apply client-side sort (non-Trakt sources, or Trakt with default sort) ─
    type_counts = {}
    for i in items_all:
        type_counts[i['type']] = type_counts.get(i['type'], 0) + 1
    logger.info(f"[PlexCollections] {source_id}: items_all={len(items_all)} type breakdown={type_counts}")
    movies_raw = [i for i in items_all if i['type'] == 'movie']
    if source_type == 'Trakt Lists' and sort_option not in ('default', ''):
        movies = movies_raw  # already in Trakt server-side order
    else:
        movies = _apply_sort(movies_raw, sort_option, sort_how)

    # ── Show resolution (cached) ──────────────────────────────────────────────
    episode_imdb_ids = {i['imdb_id'] for i in items_all if i['type'] == 'episode' and i['imdb_id']}
    _show_rk_cache: dict = {}

    if episode_imdb_ids:
        show_section_key = show_section_keys[0] if show_section_keys else _find_section_key(sections, 'show')
        if show_section_key:
            cache_key = (plex_url, show_section_key)
            now = time.time()
            cached = _show_imdb_cache.get(cache_key)
            if cached and (now - cached['ts']) < _SHOW_CACHE_TTL:
                _show_rk_cache = {imdb: cached['data'][imdb]
                                  for imdb in episode_imdb_ids
                                  if imdb in cached['data']}
                logger.debug(f"[PlexCollections] Show cache hit: resolved {len(_show_rk_cache)}/{len(episode_imdb_ids)}")
            else:
                logger.info(f"[PlexCollections] Fetching all shows from section {show_section_key} to resolve {len(episode_imdb_ids)} IMDb IDs")
                try:
                    resp = requests.get(
                        f"{plex_url}/library/sections/{show_section_key}/all",
                        headers=_headers(plex_token),
                        params={'type': 2, 'includeGuids': 1},
                        timeout=30
                    )
                    if resp.status_code == 200:
                        all_shows = resp.json().get('MediaContainer', {}).get('Metadata', [])
                        full_map = {}
                        for item in all_shows:
                            rk = str(item.get('ratingKey', ''))
                            for guid in item.get('Guid', []):
                                gid = guid.get('id', '')
                                if gid.startswith('imdb://'):
                                    full_map[gid[7:]] = rk
                        _show_imdb_cache[cache_key] = {'data': full_map, 'ts': now}
                        _show_rk_cache = {imdb: full_map[imdb] for imdb in episode_imdb_ids if imdb in full_map}
                        logger.info(f"[PlexCollections] Got {len(all_shows)} shows, resolved {len(_show_rk_cache)}/{len(episode_imdb_ids)}")
                except Exception as e:
                    logger.warning(f"[PlexCollections] Bulk show lookup failed: {e}")

    shows_raw = []
    seen_show_imdb = set()
    for i in items_all:
        if i['type'] == 'episode' and i['imdb_id'] not in seen_show_imdb:
            rk = _show_rk_cache.get(i['imdb_id'])
            if rk:
                shows_raw.append({'imdb_id': i['imdb_id'], 'ms_item_id': rk,
                                   'title': i.get('title'), 'year': i.get('year'),
                                   'release_date': i.get('release_date'),
                                   'collected_at': i.get('collected_at'),
                                   'runtime': i.get('runtime'),
                                   'type': 'show'})
                seen_show_imdb.add(i['imdb_id'])

    if source_type == 'Trakt Lists' and sort_option not in ('default', ''):
        shows = shows_raw  # already in Trakt server-side order from items_all re-ordering above
    else:
        shows = _apply_sort(shows_raw, sort_option, sort_how)

    # ── Respect media_type setting — only create collections for selected types ──
    source_media_type = source_data.get('media_type', 'All')
    if source_media_type == 'Movies':
        shows = []
    elif source_media_type == 'Shows':
        movies = []

    # ── Diagnostic summary: pipeline reconciliation ───────────────────────────
    _pairs_total   = len(ordered_pairs) if ordered_pairs else len(ordered_imdb_ids or [])
    _db_collected  = len(items_all)
    _not_collected = _pairs_total - _db_collected if _pairs_total else 0
    _movies_count  = len(movies)
    _shows_count   = len(shows)
    _media_dropped = _db_collected - _movies_count - _shows_count
    notes = []
    if _not_collected > 0:
        notes.append(f"{_not_collected} not yet collected/in DB")
    if _media_dropped > 0:
        notes.append(f"{_media_dropped} dropped by media_type filter ({source_media_type})")
    if not ordered_pairs and not ordered_imdb_ids:
        notes.append("no ordered pairs — used DB source query")
    note_str = '; '.join(notes) if notes else 'all accounted for'
    logger.info(
        f"[PlexCollections] PIPELINE {source_id}: "
        f"pairs={_pairs_total} → collected={_db_collected} → movies={_movies_count} shows={_shows_count} "
        f"| {note_str}"
    )

    # ── Determine collection names ─────────────────────────────────────────────
    base_name = coll_cfg.get('collection_name', '').strip()
    if not base_name:
        base_name = source_data.get('display_name', source_id.split('_')[0])
    sort_prefix = coll_cfg.get('sort_prefix', '!')
    is_mixed = bool(movies) and bool(shows)

    movies_override = coll_cfg.get('collection_name_movies', '').strip()
    shows_override  = coll_cfg.get('collection_name_shows', '').strip()
    if is_mixed:
        movie_coll_name = movies_override or f"{base_name} Movies"
        show_coll_name  = shows_override  or f"{base_name} Shows"
    else:
        movie_coll_name = movies_override or base_name
        show_coll_name  = shows_override  or base_name

    sync_state = _get_sync_state(source_id)
    # movie_rk / show_rk track the first synced ratingkey per type (used for poster thumb fetching)
    # Seed from per-section table for the first selected section, not from legacy state
    movie_rk = _get_ratingkey_for_section(source_id, movie_section_keys[0], 'movie') if movie_section_keys else None
    show_rk  = _get_ratingkey_for_section(source_id, show_section_keys[0],  'show')  if show_section_keys  else None

    # Persist sort settings now so check_and_sync_if_needed sees them even if sync fails partway
    _update_source_state(source_id, {'sort_option': sort_option, 'sort_how': sort_how})

    def _sync_items_to_section(items, section_key, coll_name, lib_type, label):
        """Sync a list of collected items into a named collection in one Plex library section."""
        nonlocal movie_rk, show_rk
        rk = _get_ratingkey_for_section(source_id, section_key, lib_type)
        # Migrate legacy single-library ratingkey only if it belongs to this section
        if not rk:
            legacy = sync_state.get(f'{lib_type}_collection_ratingkey')
            if legacy:
                legacy_section = _find_section_key(sections, lib_type)
                if legacy_section == section_key:
                    rk = legacy
        try:
            rk = _get_or_create_collection(plex_url, plex_token, section_key, coll_name, lib_type, sort_prefix) if not rk else rk
            _save_ratingkey_for_section(source_id, section_key, lib_type, rk)
            # Keep legacy field in sync with first section for poster code below
            if lib_type == 'movie' and not movie_rk:
                movie_rk = rk
            if lib_type == 'show' and not show_rk:
                show_rk = rk
            _rename_collection(plex_url, plex_token, section_key, rk, coll_name, sort_prefix)
            # Resolve section-local ratingKeys — ms_item_id values may come from a
            # different library. Fetch all items in this section keyed by IMDB/TMDB ID
            # so we only add items that actually exist here, using the correct ratingKey.
            id_map = _get_section_id_map(plex_url, plex_token, section_key, lib_type)
            if not id_map and items:
                logger.warning(f"[PlexCollections] {label} section={section_key}: id_map empty (fetch failed?), skipping sync to avoid data loss")
                return
            desired = []
            for i in items:
                imdb = i.get('imdb_id') or ''
                tmdb = str(i.get('tmdb_id') or '')
                local_rk = id_map.get(imdb) or id_map.get('tmdb:' + tmdb) if (imdb or tmdb) else None
                if local_rk:
                    desired.append(local_rk)
            logger.info(f"[PlexCollections] {label} section={section_key}: resolved {len(desired)}/{len(items)} items to local ratingKeys")
            current_list = _get_collection_items(plex_url, plex_token, rk)
            if not current_list and rk:
                verify = requests.get(f"{plex_url}/library/collections/{rk}", headers=_headers(plex_token), timeout=5)
                if verify.status_code == 404:
                    logger.info(f"[PlexCollections] {label} collection {rk} not found in section {section_key}, recreating")
                    rk = _create_collection(plex_url, plex_token, section_key, coll_name, lib_type, sort_prefix)
                    _save_ratingkey_for_section(source_id, section_key, lib_type, rk)
                    current_list = []
            current = set(current_list)
            to_add    = [k for k in desired if k not in current]
            to_remove = current - set(desired)
            notes = []
            if to_add:    notes.append(f"+{len(to_add)}")
            if to_remove: notes.append(f"-{len(to_remove)}")
            if not notes: notes.append("already in sync")
            logger.info(f"[PlexCollections] {label} '{coll_name}' section={section_key} ({source_id}): "
                        f"desired={len(desired)} current={len(current)} | {'; '.join(notes)}")
            if to_add:
                _add_items_to_collection(plex_url, plex_token, machine_id, rk, to_add)
            for rk_item in to_remove:
                _remove_item_from_collection(plex_url, plex_token, rk, rk_item)
            if sort_option != 'default':
                native_mode = _PLEX_NATIVE_SORT_MAP.get(sort_option)
                if native_mode is not None:
                    _set_collection_sort_mode(plex_url, plex_token, rk, native_mode)
                else:
                    _arrange_items_in_order(plex_url, plex_token, rk, desired)
            logger.info(f"[PlexCollections] {label} sync done for {source_id} section={section_key}: +{len(to_add)} -{len(to_remove)}")
        except Exception as e:
            logger.error(f"[PlexCollections] {label} sync failed for {source_id} section={section_key}: {e}", exc_info=True)

    # ── Sync movies ───────────────────────────────────────────────────────────
    if movies:
        if not movie_section_keys:
            logger.warning(f"[PlexCollections] No movie library section found/selected, skipping movies for {source_id}")
        for section_key in movie_section_keys:
            _sync_items_to_section(movies, section_key, movie_coll_name, 'movie', 'MOVIES')

    # ── Sync shows ────────────────────────────────────────────────────────────
    if shows:
        if not show_section_keys:
            logger.warning(f"[PlexCollections] No show library section found/selected, skipping shows for {source_id}")
        for section_key in show_section_keys:
            _sync_items_to_section(shows, section_key, show_coll_name, 'show', 'SHOWS')

    # ── Custom poster generation ───────────────────────────────────────────────
    poster_design = int(coll_cfg.get('poster_design', 0))
    if poster_design == 0:
        # User selected "Plex Default" — restore Plex's auto-generated composite.
        # Same approach as overlay removal: find metadata:// or composite poster,
        # download it and re-upload so it becomes the selected poster.
        try:
            import requests as _req
            _headers_json = {'X-Plex-Token': plex_token, 'Accept': 'application/json'}
            # Collect all ratingkeys across every synced section
            _all_rks = set(filter(None, [movie_rk, show_rk]))
            for _sk in movie_section_keys:
                _rk2 = _get_ratingkey_for_section(source_id, _sk, 'movie')
                if _rk2: _all_rks.add(_rk2)
            for _sk in show_section_keys:
                _rk2 = _get_ratingkey_for_section(source_id, _sk, 'show')
                if _rk2: _all_rks.add(_rk2)
            for _rk in _all_rks:
                try:
                    r = _req.get(f"{plex_url}/library/metadata/{_rk}/posters",
                                 headers=_headers_json, timeout=30)
                    if r.status_code != 200:
                        continue
                    posters = r.json().get('MediaContainer', {}).get('Metadata', [])
                    # Prefer composite (Plex auto-generated) then metadata://
                    clean = next(
                        (p for p in posters if p.get('ratingKey', '').startswith('default://')), None
                    ) or next(
                        (p for p in posters if p.get('ratingKey', '').startswith('metadata://')), None
                    ) or next(
                        (p for p in posters if not p.get('ratingKey', '').startswith('upload://')), None
                    )
                    if clean:
                        # Download the clean poster image
                        img_r = _req.get(f"{plex_url}{clean['key']}",
                                         headers={'X-Plex-Token': plex_token}, timeout=15)
                        if img_r.status_code == 200 and len(img_r.content) > 5120:
                            # Re-upload it — this replaces the selected poster
                            _req.post(f"{plex_url}/library/metadata/{_rk}/posters",
                                      data=img_r.content,
                                      headers={'X-Plex-Token': plex_token, 'Content-Type': 'image/jpeg'},
                                      timeout=30)
                            logger.info(f"[PlexCollections] Restored Plex default poster for collection {_rk}")
                except Exception as _re:
                    logger.warning(f"[PlexCollections] Could not restore default poster for {_rk}: {_re}")
            _update_source_state(source_id, {'poster_hash': '', 'poster_has_thumbs': False})
            logger.info(f"[PlexCollections] Restored Plex Default for {source_id}")
        except Exception as e:
            logger.warning(f"[PlexCollections] Failed to restore Plex Default for {source_id}: {e}")
    elif poster_design > 0:
        try:
            from database.collection_poster_renderer import (
                render_collection_poster, upload_collection_poster,
                fetch_movie_thumbs, compute_poster_hash, get_source_icon
            )
            from database.collection_poster_renderer import DESIGNS as _DESIGNS
            poster_accent = coll_cfg.get('poster_accent', '').strip()
            if not poster_accent or poster_accent.lower() in ('#000000', '000000'):
                poster_accent = _DESIGNS.get(poster_design, {}).get('default_accent', '#E6A800') or '#E6A800'
            poster_eyebrow         = coll_cfg.get('poster_eyebrow', '') or ''
            poster_icon            = coll_cfg.get('poster_icon', '') or ''
            poster_overlay_opacity = int(coll_cfg.get('poster_overlay_opacity', 60))
            poster_glow_opacity    = int(coll_cfg.get('poster_glow_opacity', 80))
            poster_glow_radius     = int(coll_cfg.get('poster_glow_radius', 55))

            # Collect first 4 ms_item_ids for hash comparison
            all_ms = [str(i.get('ms_item_id', '')) for i in (movies + shows)[:4]]
            new_poster_hash = compute_poster_hash(
                poster_design, poster_accent, poster_eyebrow,
                poster_icon, base_name, all_ms,
                poster_overlay_opacity, poster_glow_opacity, poster_glow_radius
            )
            source_state = _get_source_state(source_id)
            old_poster_hash = source_state.get('poster_hash', '')

            # Force re-render if previous poster had no real thumbs but we might have them now.
            # Default to True when key missing — only force retry when explicitly saved as False.
            old_has_thumbs = source_state.get('poster_has_thumbs', True)
            hash_changed = (new_poster_hash != old_poster_hash)

            if hash_changed or not old_has_thumbs:
                logger.info(f"[PlexCollections] Generating poster design {poster_design} for {source_id} "
                            f"(hash_changed={hash_changed}, old_has_thumbs={old_has_thumbs})")
                # Check if any collection has real thumbs (used for hash/retry logic)
                _sample_thumbs = fetch_movie_thumbs(plex_url, plex_token, movie_rk or show_rk, limit=4, tmdb_map=tmdb_map) if (movie_rk or show_rk) else [None]*4
                has_real_thumbs = any(t is not None for t in _sample_thumbs)
                logger.info(f"[PlexCollections] Fetched thumbs for {source_id}: {sum(1 for t in _sample_thumbs if t is not None)}/4 real")

                # Skip upload if hash unchanged AND we still have no real thumbs (nothing improved)
                if not hash_changed and not has_real_thumbs:
                    logger.debug(f"[PlexCollections] Poster still has no thumbs for {source_id}, skipping")
                else:
                    any_uploaded = False
                    # For mixed lists render separate posters per collection with correct name + thumbs
                    targets = []
                    if movie_rk:
                        movie_thumbs_for_coll = fetch_movie_thumbs(plex_url, plex_token, movie_rk, limit=4, tmdb_map=tmdb_map) if movie_rk else [None]*4
                        targets.append((movie_rk, movie_coll_name, movie_thumbs_for_coll))
                    if show_rk:
                        show_thumbs_for_coll = fetch_movie_thumbs(plex_url, plex_token, show_rk, limit=4, tmdb_map=tmdb_map) if show_rk else [None]*4
                        targets.append((show_rk, show_coll_name, show_thumbs_for_coll))

                    # Expand targets to cover all synced sections (not just first-of-type)
                    expanded_targets = []
                    for _base_rk, _coll_name, _thumbs in targets:
                        expanded_targets.append((_base_rk, _coll_name, _thumbs))
                        # Find all other sections that were synced for this collection name
                        for _sk in (movie_section_keys if _coll_name == movie_coll_name else show_section_keys):
                            _lib_type = 'movie' if _coll_name == movie_coll_name else 'show'
                            _extra_rk = _get_ratingkey_for_section(source_id, _sk, _lib_type)
                            if _extra_rk and _extra_rk != _base_rk:
                                _extra_thumbs = fetch_movie_thumbs(plex_url, plex_token, _extra_rk, limit=4, tmdb_map=tmdb_map)
                                expanded_targets.append((_extra_rk, _coll_name, _extra_thumbs))
                    targets = expanded_targets

                    _plex_upload_hashes = {}
                    for _rk, _coll_name, _thumbs in targets:
                        _poster_bytes = render_collection_poster(
                            design_id=poster_design,
                            collection_name=_coll_name,
                            eyebrow=poster_eyebrow,
                            accent=poster_accent,
                            icon_override=poster_icon,
                            source_type=source_type,
                            movie_thumbs=_thumbs,
                            overlay_opacity=poster_overlay_opacity,
                            glow_opacity=poster_glow_opacity,
                            glow_radius=poster_glow_radius,
                        )
                        if _poster_bytes:
                            _upload_hash = upload_collection_poster(plex_url, plex_token, _rk, _poster_bytes)
                            if _upload_hash:
                                _plex_upload_hashes[_rk] = _upload_hash
                                any_uploaded = True

                    if any_uploaded:
                        _update_source_state(source_id, {
                            'poster_hash': new_poster_hash,
                            'poster_has_thumbs': has_real_thumbs,
                            'plex_upload_hashes': _plex_upload_hashes,
                        })
                        logger.info(f"[PlexCollections] Poster applied for {source_id} (has_thumbs={has_real_thumbs})")
                    else:
                        logger.warning(f"[PlexCollections] Poster render returned None for {source_id}")
            else:
                logger.debug(f"[PlexCollections] Poster unchanged for {source_id}, skipping")
        except Exception as e:
            logger.error(f"[PlexCollections] Poster generation failed for {source_id}: {e}", exc_info=True)

    # ── Persist state ─────────────────────────────────────────────────────────
    new_fp = _compute_fingerprint(source_id, ordered_imdb_ids)
    _save_sync_state(source_id, movie_rk, show_rk, new_fp, sort_option)

    # Update state file with ordered_pairs + sort settings + timestamps
    # Only save ordered_pairs when non-empty — empty means Trakt cache-filtered all items,
    # not that the list is actually empty. Saving [] causes membership_changed=True every run.
    state_update = {
        'sort_option': sort_option,
        'sort_how': sort_how,
        'last_synced_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    if ordered_pairs:
        state_update['ordered_pairs'] = [[p[0], p[1]] for p in ordered_pairs]
    if source_type == 'Trakt Lists' and trakt_lists_url:
        updated_at = _fetch_trakt_list_updated_at(trakt_lists_url)
        if updated_at:
            state_update['trakt_updated_at'] = updated_at
    _update_source_state(source_id, state_update)


# ── Public entry point ────────────────────────────────────────────────────────

def check_and_sync_if_needed(source_id: str, source_data: dict, ordered_imdb_ids: list = None, ordered_pairs: list = None) -> None:
    """
    Efficient sync entry point. Uses state file for fast diff:
    - If membership unchanged AND sort unchanged AND (Trakt updated_at unchanged): skip entirely
    - Otherwise run full sync

    Thread-safe: concurrent calls for the same source_id are dropped.
    """
    # Re-read plex_collection config fresh from settings file — the cached source_data
    # may be stale if settings were changed after the task started
    try:
        from utilities.settings import get_all_settings as _get_all_settings
        _live_cfg = _get_all_settings().get('Content Sources', {}).get(source_id, {})
        coll_cfg = _live_cfg.get('plex_collection', {})
        if not isinstance(coll_cfg, dict):
            coll_cfg = {}
        # Merge: live config takes priority, fall back to source_data
        source_data = dict(source_data)
        source_data['plex_collection'] = coll_cfg
    except Exception:
        coll_cfg = source_data.get('plex_collection', {})
        if not isinstance(coll_cfg, dict):
            coll_cfg = {}

    if not coll_cfg.get('enabled', False):
        return

    sort_option = coll_cfg.get('sort_by', 'default')
    sort_how = coll_cfg.get('sort_how', 'asc')
    source_type = source_data.get('type', '')

    with _sync_lock:
        if source_id in _sync_in_flight:
            logger.debug(f"[PlexCollections] Sync already in flight for {source_id}, skipping")
            return
        _sync_in_flight.add(source_id)

    try:
        source_state = _get_source_state(source_id)
        old_pairs = source_state.get('ordered_pairs', [])
        old_sort_option = source_state.get('sort_option', 'default')
        old_sort_how = source_state.get('sort_how', 'asc')

        # Compute membership diff using ordered_pairs keys
        new_list_keys = {p[0] or p[1] for p in (ordered_pairs or []) if p[0] or p[1]}
        old_list_keys = {p[0] or p[1] for p in old_pairs if p[0] or p[1]}
        membership_changed = (new_list_keys != old_list_keys)
        sort_changed = (sort_option != old_sort_option or sort_how != old_sort_how)

        # Random sort must re-shuffle on every trigger — never skip
        if sort_option == 'random':
            logger.info(f"[PlexCollections] Sync needed for {source_id} (random sort — always re-shuffles)")
            sync_collection_for_source(source_id, source_data, ordered_imdb_ids, ordered_pairs)
            return

        if not membership_changed and not sort_changed:
            # Poster hash is cleared at settings-save time when poster settings change,
            # so if hash is missing here it means settings were changed — force sync.
            poster_design = int(coll_cfg.get('poster_design', 0))
            if poster_design > 0 and not source_state.get('poster_hash'):
                logger.info(f"[PlexCollections] Poster hash missing for {source_id}, forcing sync")
            else:
                # Fast path: check Trakt updated_at for Trakt sources
                if source_type == 'Trakt Lists':
                    trakt_url = source_data.get('trakt_lists', '')
                    if trakt_url:
                        live_updated_at = _fetch_trakt_list_updated_at(trakt_url)
                        stored_updated_at = source_state.get('trakt_updated_at')
                        if live_updated_at and stored_updated_at and live_updated_at == stored_updated_at:
                            logger.debug(f"[PlexCollections] Trakt list unchanged for {source_id}, skipping sync")
                            return

                # Final fallback: check DB fingerprint
                current_fp = _compute_fingerprint(source_id, ordered_imdb_ids)
                db_state = _get_sync_state(source_id)
                if db_state.get('last_fingerprint') == current_fp:
                    logger.debug(f"[PlexCollections] Fingerprint unchanged for {source_id}, skipping sync")
                    return

        logger.info(f"[PlexCollections] Sync needed for {source_id} "
                    f"(membership_changed={membership_changed}, sort_changed={sort_changed})")
        sync_collection_for_source(source_id, source_data, ordered_imdb_ids, ordered_pairs)

    except Exception as e:
        logger.error(f"[PlexCollections] check_and_sync_if_needed failed for {source_id}: {e}", exc_info=True)
    finally:
        with _sync_lock:
            _sync_in_flight.discard(source_id)
