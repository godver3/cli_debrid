"""
Plex Movie Box Sets

Discovers TMDB collection membership for all movies in the database,
creates/maintains Plex collections, applies TMDB collection posters (or a
rendered fallback), and optionally queues missing collection movies to wanted.

State file: {USER_CONFIG}/plex_boxsets_state.json
{
  "last_run": "2026-04-25T22:00:00",
  "collection_fingerprints": {
    "<tmdb_collection_id>": "<md5_of_sorted_owned_imdb_ids>"
  }
}
"""

import hashlib
import json
import logging
import os
import time
import threading
import urllib.parse
from datetime import datetime
from typing import Dict, List, Optional

import requests

from utilities.settings import get_setting

logger = logging.getLogger(__name__)

_STATE_FILE = os.path.join(os.environ.get('USER_CONFIG', '/user/config'), 'plex_boxsets_state.json')
_state_lock = threading.Lock()

TMDB_BASE = 'https://api.themoviedb.org/3'
TMDB_IMAGE_BASE = 'https://image.tmdb.org/t/p/w500'
BOXSET_ICON = 'static/color-icon.png'

# Suffixes stripped from TMDB collection names before applying the user pattern.
# e.g. "The Godfather Collection" → "The Godfather" → "{title} Collection" → "The Godfather Collection"
_COLLECTION_SUFFIXES = (
    ' collection', ' box set', ' box sets', ' saga',
    ' series', ' trilogy', ' universe', ' franchise',
)


# ── State helpers ─────────────────────────────────────────────────────────────

def _load_state() -> dict:
    with _state_lock:
        try:
            with open(_STATE_FILE, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {'last_run': None, 'collection_fingerprints': {}}


def _save_state(state: dict) -> None:
    with _state_lock:
        tmp = _STATE_FILE + '.tmp'
        try:
            with open(tmp, 'w') as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, _STATE_FILE)
        except Exception as e:
            logger.error(f"[BoxSets] Failed to save state: {e}")


# ── Naming helpers ────────────────────────────────────────────────────────────

def _strip_collection_suffix(name: str) -> str:
    """Remove trailing collection-type words from a TMDB collection name."""
    lower = name.lower()
    for suffix in _COLLECTION_SUFFIXES:
        if lower.endswith(suffix):
            return name[:len(name) - len(suffix)].strip()
    return name.strip()


def _format_collection_name(tmdb_name: str, pattern: str) -> str:
    """Apply the user's name pattern to a TMDB collection name."""
    title = _strip_collection_suffix(tmdb_name)
    return pattern.replace('{title}', title)


# ── TMDB helpers ──────────────────────────────────────────────────────────────

def _get_api_key() -> str:
    return get_setting('TMDB', 'api_key', '')


def _tmdb_get(path: str, params: dict = None) -> Optional[dict]:
    """Single TMDB GET call. Returns parsed JSON or None on any error."""
    api_key = _get_api_key()
    if not api_key:
        logger.warning("[BoxSets] TMDB API key not configured")
        return None
    p = dict(params or {})
    p['api_key'] = api_key
    p.setdefault('language', 'en-US')
    try:
        resp = requests.get(f"{TMDB_BASE}{path}", params=p, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        logger.debug(f"[BoxSets] TMDB {path} returned {resp.status_code}")
        return None
    except Exception as e:
        logger.warning(f"[BoxSets] TMDB request failed for {path}: {e}")
        return None


# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_db():
    from database.database_reading import get_db_connection
    return get_db_connection()


# ── Plex helpers ──────────────────────────────────────────────────────────────

def _plex_headers(token: str) -> dict:
    return {'X-Plex-Token': token, 'Accept': 'application/json'}


def _get_machine_id(plex_url: str, token: str) -> str:
    resp = requests.get(f"{plex_url}/", headers=_plex_headers(token), timeout=15)
    resp.raise_for_status()
    return resp.json().get('MediaContainer', {}).get('machineIdentifier', '')


def _get_movie_section_key(plex_url: str, token: str) -> Optional[str]:
    """Return the key of the first movie library section found."""
    resp = requests.get(f"{plex_url}/library/sections", headers=_plex_headers(token), timeout=15)
    resp.raise_for_status()
    for d in resp.json().get('MediaContainer', {}).get('Directory', []):
        if d.get('type') == 'movie':
            return str(d['key'])
    return None


def _find_existing_collection(plex_url: str, token: str, section_key: str, name: str) -> Optional[str]:
    resp = requests.get(
        f"{plex_url}/library/sections/{section_key}/collections",
        headers=_plex_headers(token), timeout=30
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    for item in resp.json().get('MediaContainer', {}).get('Metadata', []):
        if item.get('title', '') == name:
            return str(item['ratingKey'])
    return None


# Plex collectionSort pref values (from plexapi source):
# 0 = release date (oldest first)
# 1 = alphabetical / title
# 2 = custom / manual
# Endpoint: PUT /library/metadata/{ratingKey}/prefs?collectionSort=N
_SORT_PREFS = {
    'release_date_asc': 0,
    'title_asc':        1,
    'custom':           2,
}


def _apply_collection_sort(plex_url: str, token: str, collection_rk: str, sort_order: str) -> None:
    sort_val = _SORT_PREFS.get(sort_order, 0)
    resp = requests.put(
        f"{plex_url}/library/metadata/{collection_rk}/prefs"
        f"?collectionSort={sort_val}&X-Plex-Token={token}",
        headers={'Accept': 'application/json'}, timeout=30
    )
    if resp.status_code not in (200, 201, 204):
        logger.warning(f"[BoxSets] collectionSort prefs returned {resp.status_code} for rk={collection_rk}")


def _create_collection(plex_url: str, token: str, section_key: str, name: str, sort_order: str = 'release_date_asc') -> str:
    params = {
        'type': 1,          # movie
        'title': name,
        'smart': 0,
        'sectionId': section_key,
    }
    url = f"{plex_url}/library/collections?{urllib.parse.urlencode(params)}&X-Plex-Token={token}"
    resp = requests.post(url, headers={'Accept': 'application/json'}, timeout=30)
    resp.raise_for_status()
    rk = str(resp.json()['MediaContainer']['Metadata'][0]['ratingKey'])
    _apply_collection_sort(plex_url, token, rk, sort_order)
    logger.info(f"[BoxSets] Created Plex collection '{name}' (ratingKey={rk})")
    return rk


def _get_or_create_collection(plex_url: str, token: str, section_key: str, name: str, sort_order: str = 'release_date_asc') -> str:
    rk = _find_existing_collection(plex_url, token, section_key, name)
    if rk:
        return rk
    return _create_collection(plex_url, token, section_key, name, sort_order)


def _get_collection_item_ratingkeys(plex_url: str, token: str, collection_rk: str) -> set:
    resp = requests.get(
        f"{plex_url}/library/collections/{collection_rk}/children",
        headers=_plex_headers(token), timeout=60
    )
    if resp.status_code == 404:
        return set()
    resp.raise_for_status()
    return {str(m['ratingKey']) for m in resp.json().get('MediaContainer', {}).get('Metadata', [])}


def _add_items_to_collection(plex_url: str, token: str, machine_id: str, collection_rk: str, item_rks: list) -> None:
    for i in range(0, len(item_rks), 100):
        batch = item_rks[i:i + 100]
        csv_keys = ','.join(batch)
        uri = f"server://{machine_id}/com.plexapp.plugins.library/library/metadata/{csv_keys}"
        url = f"{plex_url}/library/collections/{collection_rk}/items?uri={urllib.parse.quote(uri)}&X-Plex-Token={token}"
        resp = requests.put(url, headers={'Accept': 'application/json'}, timeout=60)
        if resp.status_code not in (200, 201):
            logger.warning(f"[BoxSets] Add items batch returned {resp.status_code} for collection {collection_rk}")


def _delete_collection(plex_url: str, token: str, collection_rk: str) -> None:
    resp = requests.delete(
        f"{plex_url}/library/collections/{collection_rk}?X-Plex-Token={token}",
        headers={'Accept': 'application/json'}, timeout=30
    )
    if resp.status_code not in (200, 204):
        logger.warning(f"[BoxSets] Delete collection {collection_rk} returned {resp.status_code}")


def _get_all_plex_collections(plex_url: str, token: str, section_key: str) -> List[dict]:
    """Return all collections in the movie section as list of {title, ratingKey}."""
    resp = requests.get(
        f"{plex_url}/library/sections/{section_key}/collections",
        headers=_plex_headers(token), timeout=30
    )
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    return [
        {'title': m.get('title', ''), 'ratingKey': str(m['ratingKey'])}
        for m in resp.json().get('MediaContainer', {}).get('Metadata', [])
    ]


# ── Phase 1: populate tmdb_collection_id in DB ───────────────────────────────

def sync_collection_data_for_movies() -> None:
    """
    For every movie in media_items without tmdb_collection_id set,
    query TMDB to find if it belongs to a collection and store the result.
    Empty string means "checked — not in any collection".
    """
    conn = _get_db()
    try:
        rows = conn.execute(
            """SELECT id, tmdb_id FROM media_items
               WHERE type = 'movie'
                 AND tmdb_id IS NOT NULL
                 AND tmdb_collection_id IS NULL""",
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        logger.info("[BoxSets] No unchecked movies found, skipping TMDB collection lookup")
        return

    logger.info(f"[BoxSets] Checking TMDB collection membership for {len(rows)} movies")
    checked = 0
    found = 0

    for row in rows:
        db_id = row['id']
        tmdb_id = row['tmdb_id']
        data = _tmdb_get(f'/movie/{tmdb_id}')
        time.sleep(0.25)  # Stay well within TMDB rate limit (40 req/10s)

        collection = data.get('belongs_to_collection') if data else None
        if collection and collection.get('id'):
            coll_id = str(collection['id'])
            coll_name = collection.get('name', '')
        else:
            coll_id = ''    # empty string = checked, not in a collection
            coll_name = ''

        conn2 = _get_db()
        try:
            conn2.execute(
                "UPDATE media_items SET tmdb_collection_id = ?, tmdb_collection_name = ? WHERE id = ?",
                (coll_id, coll_name, db_id)
            )
            conn2.commit()
        finally:
            conn2.close()

        checked += 1
        if coll_id:
            found += 1
        if checked % 50 == 0:
            logger.info(f"[BoxSets] Progress: {checked}/{len(rows)} checked, {found} in collections")

    logger.info(f"[BoxSets] TMDB lookup complete: {checked} movies checked, {found} belong to a collection")


# ── Phase 2: build boxset list ────────────────────────────────────────────────

def build_boxset_list(name_pattern: str, min_movies: int = 2) -> List[dict]:
    """
    Group collected movies by tmdb_collection_id, fetch full collection details
    from TMDB, and return structured boxset dicts. Only includes collections
    with at least min_movies owned movies.
    """
    conn = _get_db()
    try:
        rows = conn.execute(
            """SELECT tmdb_collection_id, tmdb_collection_name,
                      GROUP_CONCAT(imdb_id) as imdb_ids,
                      GROUP_CONCAT(ms_item_id) as ms_item_ids
               FROM media_items
               WHERE type = 'movie'
                 AND tmdb_collection_id != ''
                 AND tmdb_collection_id IS NOT NULL
                 AND state = 'Collected'
                 AND ms_item_id IS NOT NULL
               GROUP BY tmdb_collection_id""",
        ).fetchall()
    finally:
        conn.close()

    boxsets = []
    for row in rows:
        coll_id = row['tmdb_collection_id']
        tmdb_name = row['tmdb_collection_name'] or ''
        owned_imdb_ids = set(i for i in (row['imdb_ids'] or '').split(',') if i)
        owned_ms_ids = set(i for i in (row['ms_item_ids'] or '').split(',') if i)

        if len(owned_ms_ids) < min_movies:
            logger.debug(f"[BoxSets] Collection {coll_id} has {len(owned_ms_ids)} owned movie(s), below threshold {min_movies} — skipping")
            continue

        # Fetch full collection from TMDB to get poster_path and all parts
        coll_data = _tmdb_get(f'/collection/{coll_id}')
        time.sleep(0.25)

        if coll_data:
            # Use TMDB's authoritative name if we have it
            tmdb_name = coll_data.get('name', tmdb_name)
            poster_path = coll_data.get('poster_path')  # e.g. "/abc123.jpg" or None
            all_parts = [
                {
                    'tmdb_id': p.get('id'),
                    'title': p.get('title', ''),
                    'release_date': p.get('release_date', ''),
                }
                for p in coll_data.get('parts', [])
            ]
        else:
            # TMDB doesn't have this collection — build from DB data and use rendered poster
            logger.warning(f"[BoxSets] Could not fetch TMDB collection {coll_id} ('{tmdb_name}'), using DB name and rendered poster")
            poster_path = None
            all_parts = []

        # If we still have no collection name, fall back to the earliest released owned movie's title
        if not tmdb_name:
            conn_fb = _get_db()
            try:
                fb_row = conn_fb.execute(
                    """SELECT title FROM media_items
                       WHERE type = 'movie'
                         AND tmdb_collection_id = ?
                         AND state = 'Collected'
                         AND title IS NOT NULL
                       ORDER BY COALESCE(release_date, '9999') ASC
                       LIMIT 1""",
                    (coll_id,)
                ).fetchone()
            finally:
                conn_fb.close()
            if fb_row:
                tmdb_name = fb_row['title']
                logger.debug(f"[BoxSets] No TMDB name for collection {coll_id}, using first movie title: '{tmdb_name}'")

        if not tmdb_name:
            logger.warning(f"[BoxSets] Cannot determine name for collection {coll_id}, skipping")
            continue

        display_name = _format_collection_name(tmdb_name, name_pattern)

        boxsets.append({
            'collection_id': coll_id,
            'tmdb_name': tmdb_name,
            'display_name': display_name,
            'poster_path': poster_path,
            'owned_imdb_ids': owned_imdb_ids,
            'owned_ms_ids': owned_ms_ids,
            'all_parts': all_parts,
        })

    logger.info(f"[BoxSets] Built {len(boxsets)} boxsets from DB")
    return boxsets


# ── Phase 3: sync Plex collections ───────────────────────────────────────────

def sync_plex_collections(boxsets: List[dict], plex_url: str, token: str,
                          section_key: str, machine_id: str,
                          sort_order: str = 'release_date_asc') -> Dict[str, str]:
    """
    For each boxset, find or create a Plex collection and ensure all owned
    movies are members. Returns {collection_id: plex_rating_key}.
    """
    plex_map = {}

    for bs in boxsets:
        display_name = bs['display_name']
        owned_ms_ids = list(bs['owned_ms_ids'])
        coll_id = bs['collection_id']

        try:
            collection_rk = _get_or_create_collection(plex_url, token, section_key, display_name, sort_order)
            plex_map[coll_id] = collection_rk

            # Always re-apply sort so a settings change takes effect on existing collections
            _apply_collection_sort(plex_url, token, collection_rk, sort_order)

            # Find which owned movies are not yet in the collection
            existing_rks = _get_collection_item_ratingkeys(plex_url, token, collection_rk)
            to_add = [rk for rk in owned_ms_ids if rk not in existing_rks]

            if to_add:
                _add_items_to_collection(plex_url, token, machine_id, collection_rk, to_add)
                logger.info(f"[BoxSets] '{display_name}': added {len(to_add)} movies to Plex collection (rk={collection_rk})")
            else:
                logger.info(f"[BoxSets] '{display_name}': all {len(owned_ms_ids)} movies already in collection (rk={collection_rk})")

        except Exception as e:
            logger.error(f"[BoxSets] Failed to sync Plex collection '{display_name}': {e}", exc_info=True)

    return plex_map


# ── Phase 4: apply posters ────────────────────────────────────────────────────

def apply_boxset_posters(boxsets: List[dict], plex_map: Dict[str, str],
                         plex_url: str, token: str, state: dict) -> dict:
    """
    For each boxset with a TMDB poster: download and upload it directly.
    For boxsets without: render a Design 1 poster with the cli_debrid icon.
    Skips collections whose owned-movie fingerprint hasn't changed.
    Returns updated state dict.
    """
    from database.collection_poster_renderer import (
        render_collection_poster, upload_collection_poster, fetch_movie_thumbs
    )

    fingerprints = state.setdefault('collection_fingerprints', {})
    collection_names = state.setdefault('collection_names', {})
    collection_ratingkeys = state.setdefault('collection_ratingkeys', {})
    collection_poster_hashes = state.setdefault('collection_poster_hashes', {})

    for bs in boxsets:
        coll_id = bs['collection_id']
        collection_names[coll_id] = bs['display_name']
        collection_rk = plex_map.get(coll_id)
        if not collection_rk:
            continue

        # Fingerprint = hash of sorted owned imdb_ids + ms_item_ids
        # Including ms_item_ids ensures a re-apply when newly collected movies
        # get their Plex ratingKey populated after a Plex scan
        fp_input = ','.join(sorted(bs['owned_imdb_ids'])) + '|' + ','.join(sorted(bs['owned_ms_ids']))
        fp = hashlib.md5(fp_input.encode()).hexdigest()

        if fingerprints.get(coll_id) == fp:
            logger.info(f"[BoxSets] '{bs['display_name']}': poster unchanged, skipping")
            continue

        try:
            if bs['poster_path']:
                # Download TMDB collection poster and upload directly to Plex
                img_url = f"{TMDB_IMAGE_BASE}{bs['poster_path']}"
                resp = requests.get(img_url, timeout=30)
                if resp.status_code == 200:
                    poster_bytes = resp.content
                    upload_hash = upload_collection_poster(plex_url, token, collection_rk, poster_bytes)
                    if upload_hash:
                        logger.info(f"[BoxSets] '{bs['display_name']}': TMDB poster applied")
                        fingerprints[coll_id] = fp
                        collection_ratingkeys[coll_id] = collection_rk
                        collection_poster_hashes[coll_id] = upload_hash
                    else:
                        logger.warning(f"[BoxSets] '{bs['display_name']}': TMDB poster upload failed")
                        bs['poster_path'] = None  # Fall through to rendered poster
                else:
                    logger.warning(f"[BoxSets] '{bs['display_name']}': TMDB poster download failed ({resp.status_code}), falling back to renderer")
                    bs['poster_path'] = None  # Fall through to rendered poster

            if not bs['poster_path']:
                # Render fallback: Design 1 with cli_debrid icon, no accent/eyebrow
                thumbs = fetch_movie_thumbs(plex_url, token, collection_rk, limit=4)
                poster_bytes = render_collection_poster(
                    design_id=1,
                    collection_name=bs['display_name'],
                    eyebrow='',
                    accent='',
                    icon_override=BOXSET_ICON,
                    source_type='Box Set',
                    movie_thumbs=thumbs,
                )
                if poster_bytes:
                    upload_hash = upload_collection_poster(plex_url, token, collection_rk, poster_bytes)
                    if upload_hash:
                        logger.info(f"[BoxSets] '{bs['display_name']}': rendered fallback poster applied")
                        fingerprints[coll_id] = fp
                        collection_ratingkeys[coll_id] = collection_rk
                        collection_poster_hashes[coll_id] = upload_hash
                    else:
                        logger.warning(f"[BoxSets] '{bs['display_name']}': rendered fallback poster upload failed")
                else:
                    logger.warning(f"[BoxSets] '{bs['display_name']}': poster render returned None")

        except Exception as e:
            logger.error(f"[BoxSets] Poster failed for '{bs['display_name']}': {e}", exc_info=True)

    return state


# ── Phase 5: queue missing movies ────────────────────────────────────────────

def add_missing_movies_to_wanted(boxsets: List[dict], version: str) -> int:
    """
    For each boxset, find TMDB parts not present in media_items at all
    (any state) and add them to the wanted queue.
    Returns the count of movies queued.
    """
    from database.wanted_items import add_wanted_items

    # Collect all tmdb_ids we need to check
    all_tmdb_ids = set()
    for bs in boxsets:
        for part in bs['all_parts']:
            tid = part.get('tmdb_id')
            if tid:
                all_tmdb_ids.add(str(tid))

    if not all_tmdb_ids:
        return 0

    # Find which are already in our DB (any state)
    conn = _get_db()
    try:
        placeholders = ','.join('?' * len(all_tmdb_ids))
        existing_rows = conn.execute(
            f"SELECT DISTINCT tmdb_id FROM media_items WHERE tmdb_id IN ({placeholders})",
            list(all_tmdb_ids)
        ).fetchall()
    finally:
        conn.close()

    existing_tmdb_ids = {str(r['tmdb_id']) for r in existing_rows}
    batch = []

    for bs in boxsets:
        for part in bs['all_parts']:
            tmdb_id = part.get('tmdb_id')
            if not tmdb_id:
                continue
            if str(tmdb_id) in existing_tmdb_ids:
                continue

            # Need the IMDB ID to add to wanted queue
            ext = _tmdb_get(f'/movie/{tmdb_id}/external_ids')
            time.sleep(0.25)
            imdb_id = (ext or {}).get('imdb_id', '')
            if not imdb_id:
                logger.debug(f"[BoxSets] No IMDB ID for TMDB {tmdb_id} ('{part['title']}'), skipping")
                continue

            year = None
            if part.get('release_date') and len(part['release_date']) >= 4:
                try:
                    year = int(part['release_date'][:4])
                except ValueError:
                    pass

            batch.append({
                'imdb_id': imdb_id,
                'tmdb_id': str(tmdb_id),
                'title': part['title'],
                'year': year,
                'release_date': part.get('release_date'),
                'content_source': 'Plex Box Sets',
                'content_source_detail': bs['display_name'],
            })
            logger.info(f"[BoxSets] Queuing missing movie: '{part['title']}' ({imdb_id}) for '{bs['display_name']}'")

    if batch:
        add_wanted_items(batch, versions_input={version: True}, unblacklist=False)
        logger.info(f"[BoxSets] Queued {len(batch)} missing movies to wanted")
    else:
        logger.info("[BoxSets] No missing movies to queue")

    return len(batch)


# ── Cleanup: remove Plex collections below threshold ─────────────────────────

def cleanup_collections_below_threshold(plex_url: str, token: str, section_key: str,
                                        min_movies: int, state: dict) -> dict:
    """
    Find all managed Plex collections (those whose names match a collection in our
    DB) that now have fewer than min_movies owned movies. Delete them from Plex and
    remove their fingerprints from state.
    """
    conn = _get_db()
    try:
        # Find all collection IDs that have at least 1 but fewer than min_movies owned movies
        rows = conn.execute(
            """SELECT tmdb_collection_id, tmdb_collection_name,
                      COUNT(*) as owned_count
               FROM media_items
               WHERE type = 'movie'
                 AND tmdb_collection_id != ''
                 AND tmdb_collection_id IS NOT NULL
                 AND state = 'Collected'
                 AND ms_item_id IS NOT NULL
               GROUP BY tmdb_collection_id
               HAVING owned_count < ?""",
            (min_movies,)
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        logger.info("[BoxSets] No collections below threshold to clean up")
        return state

    name_pattern = get_setting('Plex Movie Box Sets', 'collection_name_pattern', '{title} Collection') or '{title} Collection'

    # Get all current Plex collections once
    try:
        all_plex = _get_all_plex_collections(plex_url, token, section_key)
    except Exception as e:
        logger.error(f"[BoxSets] Cannot fetch Plex collections for cleanup: {e}")
        return state

    plex_by_title = {c['title']: c['ratingKey'] for c in all_plex}
    fingerprints = state.setdefault('collection_fingerprints', {})
    deleted = 0

    for row in rows:
        coll_id = row['tmdb_collection_id']
        tmdb_name = row['tmdb_collection_name'] or ''
        if not tmdb_name:
            continue
        display_name = _format_collection_name(tmdb_name, name_pattern)
        rk = plex_by_title.get(display_name)
        if rk:
            try:
                _delete_collection(plex_url, token, rk)
                logger.info(f"[BoxSets] Deleted Plex collection '{display_name}' (owned {row['owned_count']} < threshold {min_movies})")
                deleted += 1
            except Exception as e:
                logger.error(f"[BoxSets] Failed to delete collection '{display_name}': {e}")
        else:
            logger.debug(f"[BoxSets] Collection '{display_name}' not found in Plex, nothing to delete")

        # Remove fingerprint regardless so it won't be stale if threshold changes later
        fingerprints.pop(coll_id, None)

    if deleted:
        logger.info(f"[BoxSets] Cleanup complete — deleted {deleted} Plex collection(s) below threshold")
    return state


# ── Entry point ───────────────────────────────────────────────────────────────

def run_plex_movie_boxsets() -> None:
    """Main entry point called by the scheduled task and the Run Now button."""
    logger.info("[BoxSets] Task started")

    # Guards
    if not get_setting('Plex Movie Box Sets', 'enabled', False):
        logger.info("[BoxSets] Feature disabled in settings, skipping")
        return

    plex_url = get_setting('Plex', 'url', '').rstrip('/')
    plex_token = get_setting('Plex', 'token', '')
    if not plex_url or not plex_token:
        logger.error("[BoxSets] Plex URL or token not configured, skipping")
        return

    if not _get_api_key():
        logger.error("[BoxSets] TMDB API key not configured, skipping")
        return

    name_pattern = get_setting('Plex Movie Box Sets', 'collection_name_pattern', '{title} Collection') or '{title} Collection'
    grab_missing = get_setting('Plex Movie Box Sets', 'grab_missing', False)
    grab_version = get_setting('Plex Movie Box Sets', 'grab_version', 'Default') or 'Default'
    min_movies = int(get_setting('Plex Movie Box Sets', 'min_movies', 2) or 2)
    sort_order = get_setting('Plex Movie Box Sets', 'sort_order', 'release_date_asc') or 'release_date_asc'

    # Resolve Plex library
    try:
        section_key = _get_movie_section_key(plex_url, plex_token)
        if not section_key:
            logger.error("[BoxSets] No movie library section found in Plex, skipping")
            return
        machine_id = _get_machine_id(plex_url, plex_token)
    except Exception as e:
        logger.error(f"[BoxSets] Cannot connect to Plex: {e}")
        return

    state = _load_state()

    try:
        # Cleanup: remove Plex collections that now fall below the minimum threshold
        state = cleanup_collections_below_threshold(plex_url, plex_token, section_key, min_movies, state)
        _save_state(state)

        # Phase 1: populate DB with TMDB collection membership (incremental)
        sync_collection_data_for_movies()

        # Phase 2: group owned movies into boxsets (enforces min_movies threshold)
        boxsets = build_boxset_list(name_pattern, min_movies=min_movies)
        if not boxsets:
            logger.info("[BoxSets] No box sets found in library, done")
            state['last_run'] = datetime.utcnow().isoformat()
            _save_state(state)
            return

        # Phase 3: create/update Plex collections
        plex_map = sync_plex_collections(boxsets, plex_url, plex_token, section_key, machine_id, sort_order=sort_order)

        # Phase 4: apply posters
        state = apply_boxset_posters(boxsets, plex_map, plex_url, plex_token, state)

        # Phase 5: queue missing movies (optional)
        queued = 0
        if grab_missing:
            queued = add_missing_movies_to_wanted(boxsets, grab_version)

        state['last_run'] = datetime.utcnow().isoformat()
        _save_state(state)

        logger.info(
            f"[BoxSets] Done — {len(boxsets)} collections synced, "
            f"{len(plex_map)} Plex collections updated, "
            f"{queued} missing movies queued"
        )

    except Exception as e:
        logger.error(f"[BoxSets] Task failed: {e}", exc_info=True)
        state['last_run'] = datetime.utcnow().isoformat()
        _save_state(state)


def reapply_single_collection_poster(collection_id: str) -> dict:
    """
    Immediately reapply the poster for one box set collection.
    Clears its fingerprint, fetches the boxset from DB, and applies the poster.
    Returns {'success': True/False, 'message': str}.
    """
    from utilities.settings import get_setting
    from database.collection_poster_renderer import (
        render_collection_poster, upload_collection_poster, fetch_movie_thumbs
    )

    plex_url = get_setting('Plex', 'url', '').rstrip('/')
    plex_token = get_setting('Plex', 'token', '')
    if not plex_url or not plex_token:
        return {'success': False, 'message': 'Plex URL or token not configured'}
    if not _get_api_key():
        return {'success': False, 'message': 'TMDB API key not configured'}

    name_pattern = get_setting('Plex Movie Box Sets', 'collection_name_pattern', '{title} Collection') or '{title} Collection'
    min_movies = int(get_setting('Plex Movie Box Sets', 'min_movies', 2) or 2)
    sort_order = get_setting('Plex Movie Box Sets', 'sort_order', 'release_date_asc') or 'release_date_asc'

    try:
        section_key = _get_movie_section_key(plex_url, plex_token)
        if not section_key:
            return {'success': False, 'message': 'No movie library section found in Plex'}
        machine_id = _get_machine_id(plex_url, plex_token)
    except Exception as e:
        return {'success': False, 'message': f'Cannot connect to Plex: {e}'}

    # Query DB for just this collection — avoids iterating all 295 collections with TMDB API calls
    conn = _get_db()
    try:
        row = conn.execute(
            """SELECT tmdb_collection_id, tmdb_collection_name,
                      GROUP_CONCAT(imdb_id) as imdb_ids,
                      GROUP_CONCAT(ms_item_id) as ms_item_ids
               FROM media_items
               WHERE type = 'movie'
                 AND tmdb_collection_id = ?
                 AND state = 'Collected'
                 AND ms_item_id IS NOT NULL
               GROUP BY tmdb_collection_id""",
            (collection_id,)
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return {'success': False, 'message': 'Collection not found — it may be below the minimum movies threshold or have no ms_item_id set yet'}

    owned_imdb_ids = set(i for i in (row['imdb_ids'] or '').split(',') if i)
    owned_ms_ids = set(i for i in (row['ms_item_ids'] or '').split(',') if i)

    if len(owned_ms_ids) < min_movies:
        return {'success': False, 'message': f'Collection has only {len(owned_ms_ids)} owned movie(s), below the minimum threshold of {min_movies}'}

    # Fetch just this TMDB collection
    coll_data = _tmdb_get(f'/collection/{collection_id}')
    if not coll_data:
        return {'success': False, 'message': f'Could not fetch TMDB collection {collection_id}'}

    tmdb_name = coll_data.get('name', row['tmdb_collection_name'] or '')
    if not tmdb_name:
        return {'success': False, 'message': 'Cannot determine collection name'}

    display_name = _format_collection_name(tmdb_name, name_pattern)
    poster_path = coll_data.get('poster_path')
    all_parts = [
        {'tmdb_id': p.get('id'), 'title': p.get('title', ''), 'release_date': p.get('release_date', '')}
        for p in coll_data.get('parts', [])
    ]

    bs = {
        'collection_id': collection_id,
        'tmdb_name': tmdb_name,
        'display_name': display_name,
        'poster_path': poster_path,
        'owned_imdb_ids': owned_imdb_ids,
        'owned_ms_ids': owned_ms_ids,
        'all_parts': all_parts,
    }

    # Find/create the Plex collection and get its ratingKey
    try:
        collection_rk = _get_or_create_collection(plex_url, plex_token, section_key, bs['display_name'], sort_order)
    except Exception as e:
        return {'success': False, 'message': f'Plex collection lookup failed: {e}'}

    # Apply poster
    try:
        if bs['poster_path']:
            img_url = f"{TMDB_IMAGE_BASE}{bs['poster_path']}"
            resp = requests.get(img_url, timeout=30)
            if resp.status_code == 200:
                upload_collection_poster(plex_url, plex_token, collection_rk, resp.content)
            else:
                bs['poster_path'] = None  # fall through to renderer

        if not bs['poster_path']:
            thumbs = fetch_movie_thumbs(plex_url, plex_token, collection_rk, limit=4)
            poster_bytes = render_collection_poster(
                design_id=1,
                collection_name=bs['display_name'],
                eyebrow='',
                accent='',
                icon_override=BOXSET_ICON,
                source_type='Box Set',
                movie_thumbs=thumbs,
            )
            if not poster_bytes:
                return {'success': False, 'message': 'Poster render returned None'}
            upload_collection_poster(plex_url, plex_token, collection_rk, poster_bytes)

    except Exception as e:
        return {'success': False, 'message': f'Poster apply failed: {e}'}

    # Update fingerprint in state so next full run doesn't re-apply unnecessarily
    state = _load_state()
    fingerprints = state.setdefault('collection_fingerprints', {})
    collection_names = state.setdefault('collection_names', {})
    fp_input = ','.join(sorted(bs['owned_imdb_ids'])) + '|' + ','.join(sorted(bs['owned_ms_ids']))
    fingerprints[collection_id] = hashlib.md5(fp_input.encode()).hexdigest()
    collection_names[collection_id] = bs['display_name']
    _save_state(state)

    logger.info(f"[BoxSets] Force reapply: '{bs['display_name']}' poster applied successfully")
    return {'success': True, 'message': f"Poster applied for '{bs['display_name']}'"}

