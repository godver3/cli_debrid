import json
import logging
import os
import re
from typing import List, Dict, Any, Tuple, Optional

from routes.api_tracker import api
from utilities.settings import get_setting

REQUEST_TIMEOUT = 30

# Some unmatched Scrob movie entries carry a filename-style imdb tag directly
# in the title (e.g. "Top Gun (1986) - {imdb-tt0092099} - ...") instead of a
# resolved tmdb_id — Scrob itself failed to match these against TMDB, but the
# uploader's own naming convention already embeds a usable IMDb ID.
_TITLE_IMDB_TAG_RE = re.compile(r'\{imdb-(tt\d+)\}')

# Session JWT persisted to disk, mirroring content_checkers.trakt's
# .pytrakt.json approach — Scrob's login cookie is a 7-day JWT (per Scrob's
# own docs), so caching it avoids a fresh /login POST on every scheduled
# content-source run. Only used for write operations (deletion sync); all
# read/sync operations use the API key via _scrob_get above.
CONFIG_DIR = os.environ.get('USER_CONFIG', '/user/config')
SCROB_SESSION_FILE = os.path.join(CONFIG_DIR, '.scrob_session.json')

# Scrob's /media/tmdb/list and named special endpoints are proxied TMDB discover
# calls (see backend/routers/media.py in ellite/scrob) — same shape as Trakt's
# special lists, but keyed by tmdb_id rather than imdb_id, so every item here
# needs a tmdb->imdb resolution step before it can feed the shared
# add_wanted_items/process_metadata pipeline.
SPECIAL_LIST_ENDPOINTS = {
    "Trending":         {"movies": ("/media/trending/movies", None), "shows": ("/media/trending/shows", None)},
    "Popular":          {"movies": ("/media/tmdb/list", {'type': 'movie', 'category': 'popular'}), "shows": ("/media/tmdb/list", {'type': 'series', 'category': 'popular'})},
    "Top Rated":        {"movies": ("/media/top-rated-movies", None), "shows": ("/media/top-rated-shows", None)},
    "Now Playing":      {"movies": ("/media/now-playing", None), "shows": None},
    "Upcoming":         {"movies": ("/media/upcoming", None), "shows": None},
    "On Air Today":     {"movies": None, "shows": ("/media/on-air-today", None)},
    "On Air This Week": {"movies": None, "shows": ("/media/on-air-this-week", None)},
    "New Episodes":     {"movies": None, "shows": ("/media/new-episodes", None)},
    "Hidden Gems":      {"movies": ("/media/hidden-gems", None), "shows": None},
    "For You":          {"movies": ("/media/for-you", None), "shows": ("/media/for-you", None)},
    "Recently Added":   {"movies": ("/media/recently-added", None), "shows": ("/media/recently-added", None)},
}

# Movie/TV genre names matching Scrob's MOVIE_GENRE_IDS/TV_GENRE_IDS keys
# (standard TMDB genre names) — passed through to /media/tmdb/list?genre=.
GENRE_CHOICES = [
    "Action", "Adventure", "Animation", "Comedy", "Crime", "Documentary",
    "Drama", "Family", "Fantasy", "History", "Horror", "Music", "Mystery",
    "Romance", "Science Fiction", "TV Movie", "Thriller", "War", "Western",
    "Action & Adventure", "Kids", "News", "Reality", "Sci-Fi & Fantasy",
    "Soap", "Talk", "War & Politics",
]


def get_scrob_config() -> Optional[Dict[str, str]]:
    """Reads the shared Scrob connection settings (base URL + API key).

    Configured once under Additional Settings, alongside Trakt — every Scrob
    content source (Lists/Collection/Special) reuses this same connection
    instead of duplicating URL/key fields per source.
    """
    base_url = (get_setting('Scrob', 'url', '') or '').strip().rstrip('/')
    api_key = (get_setting('Scrob', 'api_key', '') or '').strip()
    if not base_url or not api_key:
        logging.debug("Scrob is not configured (missing URL or API key) — skipping.")
        return None
    return {'base_url': base_url, 'api_key': api_key}


def _scrob_get(path_or_endpoint: str, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
    """GET against Scrob's /api/proxy/... surface, using the shared API key.

    path_or_endpoint may already include its own query string (as the
    SPECIAL_LIST_ENDPOINTS table does for /media/tmdb/list) — params passed
    alongside are merged in by requests.
    """
    config = get_scrob_config()
    if not config:
        return None

    url = f"{config['base_url']}/api/proxy{path_or_endpoint}"
    headers = {'X-Api-Key': config['api_key'], 'Accept': 'application/json'}
    try:
        response = api.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching from Scrob ({url}): {e}")
        return None


def _load_cached_session_token() -> Optional[str]:
    try:
        if os.path.exists(SCROB_SESSION_FILE):
            with open(SCROB_SESSION_FILE, 'r') as f:
                data = json.load(f)
            return data.get('token')
    except Exception as e:
        logging.debug(f"Could not read cached Scrob session: {e}")
    return None


def _save_cached_session_token(token: str) -> None:
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(SCROB_SESSION_FILE, 'w') as f:
            json.dump({'token': token}, f)
    except Exception as e:
        logging.warning(f"Could not save Scrob session token: {e}")


def _login_and_get_session_token() -> Optional[str]:
    """POSTs to Scrob's /login page route (not an API path) to obtain a fresh
    session JWT, per the auth flow documented for this instance: the page sets
    an httpOnly 'token' cookie on success. Username/password are read directly
    from settings each call — never logged, never sent anywhere else.
    """
    base_url = (get_setting('Scrob', 'url', '') or '').strip().rstrip('/')
    username = (get_setting('Scrob', 'username', '') or '').strip()
    password = get_setting('Scrob', 'password', '') or ''

    if not base_url or not username or not password:
        logging.debug("Scrob username/password not configured — cannot obtain a write-capable session.")
        return None

    try:
        response = api.post(
            f"{base_url}/login",
            data={'_step': 'login', 'username': username, 'password': password},
            timeout=REQUEST_TIMEOUT,
        )
        token = response.cookies.get('token')
        if token:
            _save_cached_session_token(token)
            logging.info("Scrob login succeeded — session token cached.")
            return token
        logging.error("Scrob login failed — no session token returned (check username/password in Additional Settings → Scrob).")
        return None
    except api.exceptions.RequestException as e:
        logging.error(f"Error logging in to Scrob: {e}")
        return None


def _get_session_token(force_refresh: bool = False) -> Optional[str]:
    """Returns a usable session JWT, from cache unless force_refresh (used
    after a 401, since a cached token may have expired or been revoked)."""
    if not force_refresh:
        cached = _load_cached_session_token()
        if cached:
            return cached
    return _login_and_get_session_token()


def _scrob_write(method: str, path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
    """POST/DELETE/etc against Scrob's /api/proxy/... surface using a session
    JWT (cookie-based) — required for write endpoints, which reject the API
    key (confirmed against Scrob's own OpenAPI schema: write routes declare
    OAuth2PasswordBearer via the /auth/login password flow, not the API-key
    dependency reads use). Retries once with a fresh login if the cached
    token has expired.
    """
    base_url = (get_setting('Scrob', 'url', '') or '').strip().rstrip('/')
    if not base_url:
        return None

    url = f"{base_url}/api/proxy{path}"
    request_fn = getattr(api, method.lower())

    for attempt, force_refresh in enumerate((False, True)):
        token = _get_session_token(force_refresh=force_refresh)
        if not token:
            return None
        try:
            response = request_fn(url, params=params, cookies={'token': token}, timeout=REQUEST_TIMEOUT)
            if response.status_code == 401 and attempt == 0:
                logging.info("Scrob session token rejected (401) — retrying with a fresh login.")
                continue
            response.raise_for_status()
            return response.json() if response.content else {'status': 'ok'}
        except api.exceptions.RequestException as e:
            logging.error(f"Error calling Scrob write endpoint ({url}): {e}")
            return None
    return None


def _tmdb_to_imdb(tmdb_id: Any, media_type: str) -> Optional[str]:
    """Resolves a Scrob tmdb_id/type pair to an imdb_id via the shared metadata battery."""
    if not tmdb_id:
        return None
    try:
        from cli_battery.app.direct_api import DirectAPI
        imdb_id, _ = DirectAPI.tmdb_to_imdb(str(tmdb_id), media_type=media_type)
        return imdb_id
    except Exception as e:
        logging.debug(f"Could not resolve Scrob tmdb_id {tmdb_id} ({media_type}) to imdb_id: {e}")
        return None


def process_scrob_items(items: List[Dict[str, Any]], unblacklist: bool = False) -> List[Dict[str, Any]]:
    """Mirrors content_checkers.trakt.process_trakt_items: reduces raw Scrob
    media dicts down to the minimal {'imdb_id', 'media_type'} shape the shared
    add_wanted_items/process_metadata pipeline expects, applying the same
    ghostlist/blacklist gating Trakt sources use.
    """
    from database.core import get_db_connection

    processed_items = []
    seen_imdb_ids = set()
    skipped_count = 0
    duplicate_count = 0
    blacklisted_count = 0
    unblacklisted_count = 0

    for item in items:
        # Scrob's list-item shape wraps the media dict under 'media'; discover/
        # collection endpoints return the media dict directly.
        media = item.get('media', item)

        raw_type = (media.get('type') or '').lower()
        if raw_type in ('movie',):
            media_type = 'movie'
            tmdb_id = media.get('tmdb_id')
        elif raw_type == 'episode':
            # Episode rows carry the EPISODE's own tmdb_id in 'tmdb_id' — a
            # completely separate ID namespace from shows/movies in TMDB. Using
            # it directly would resolve to an unrelated, essentially random
            # show (confirmed: Billions episode tmdb_id 4524404 is not a valid
            # show ID at all). show_tmdb_id is the actual parent show's ID and
            # is what must be used to roll episodes up for wanted-item purposes,
            # same as Trakt episode items resolving via their show's imdb_id.
            media_type = 'tv'
            tmdb_id = media.get('show_tmdb_id')
        elif raw_type in ('series', 'show', 'tv'):
            media_type = 'tv'
            tmdb_id = media.get('tmdb_id')
        else:
            skipped_count += 1
            continue

        imdb_id = _tmdb_to_imdb(tmdb_id, media_type)
        if not imdb_id and media_type == 'movie':
            # Last-resort fallback: Scrob failed to match this file to TMDB at
            # all, but the uploader's own filename-style title tag already has
            # a usable IMDb ID — skip the TMDB round-trip entirely.
            tag_match = _TITLE_IMDB_TAG_RE.search(media.get('title') or '')
            if tag_match:
                imdb_id = tag_match.group(1)
                logging.info(f"Resolved Scrob item via imdb tag embedded in title: {media.get('title')} -> {imdb_id}")
        if not imdb_id:
            logging.warning(f"Skipping Scrob item due to unresolved imdb_id: {media.get('title') or media.get('show_title') or 'Unknown Title'} (tmdb_id={tmdb_id})")
            skipped_count += 1
            continue

        if imdb_id in seen_imdb_ids:
            duplicate_count += 1
            continue

        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT title, state, ghostlisted FROM media_items WHERE imdb_id = ? LIMIT 1",
                (imdb_id,)
            )
            row = cursor.fetchone()

            if row:
                item_title, item_state, ghostlisted_flag = row[0], row[1], row[2]
                is_ghostlisted = ghostlisted_flag == 1
                is_blacklisted = item_state == 'Blacklisted'

                if is_ghostlisted:
                    logging.info(f"⛔ BLOCKED: Skipping ghostlisted item from Scrob: {item_title} (IMDB: {imdb_id})")
                    blacklisted_count += 1
                    seen_imdb_ids.add(imdb_id)
                    continue
                elif is_blacklisted:
                    if unblacklist:
                        logging.info(f"🔓 Allowing blacklisted item through for unblacklist: {item_title} (IMDB: {imdb_id})")
                        unblacklisted_count += 1
                    else:
                        logging.info(f"⛔ BLOCKED: Skipping blacklisted item from Scrob: {item_title} (IMDB: {imdb_id})")
                        blacklisted_count += 1
                        seen_imdb_ids.add(imdb_id)
                        continue
        except Exception as e:
            logging.error(f"Error checking blacklist status for {imdb_id}: {e}")

        seen_imdb_ids.add(imdb_id)
        processed_items.append({'imdb_id': imdb_id, 'media_type': media_type})

    if skipped_count > 0:
        logging.info(f"Skipped {skipped_count} Scrob items due to missing media type or unresolved ID")
    if duplicate_count > 0:
        logging.info(f"Skipped {duplicate_count} duplicate Scrob items")
    if blacklisted_count > 0:
        logging.info(f"⛔ Blocked {blacklisted_count} blacklisted/ghostlisted items from Scrob")
    if unblacklisted_count > 0:
        logging.info(f"🔓 Passing {unblacklisted_count} blacklisted item(s) through for unblacklist processing")

    return processed_items


def get_wanted_from_scrob_lists(scrob_list_ids: str, versions: Dict[str, bool], unblacklist: bool = False) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    """Fetches items from one or more Scrob custom lists (by numeric list id).

    scrob_list_ids: comma-separated Scrob list IDs, e.g. "2,7" — mirrors the
    comma-separated trakt_lists field on Trakt Lists sources.
    """
    all_wanted_items = []
    list_ids = [lid.strip() for lid in (scrob_list_ids or '').split(',') if lid.strip()]
    if not list_ids:
        return all_wanted_items

    for list_id in list_ids:
        data = _scrob_get(f"/lists/{list_id}")
        if not data:
            logging.error(f"Failed to fetch Scrob list {list_id}")
            continue

        raw_items = data.get('items', [])
        processed_items = process_scrob_items(raw_items, unblacklist=unblacklist)
        logging.info(f"Found {len(processed_items)} items from Scrob list '{data.get('name', list_id)}'")
        all_wanted_items.append((processed_items, versions))

    return all_wanted_items


def get_wanted_from_scrob_collection(versions: Dict[str, bool], unblacklist: bool = False) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    """Fetches the user's full Scrob collection (GET /media, unfiltered — this
    endpoint is unconditionally scoped to the current user's Collection on the
    Scrob side, confirmed against backend/routers/media.py's list_media()).
    """
    all_wanted_items = []
    all_items: List[Dict[str, Any]] = []
    page = 1
    page_size = 100

    while True:
        data = _scrob_get("/media", params={'page': page, 'page_size': page_size})
        if not data:
            break
        results = data.get('results', [])
        if not results:
            break
        all_items.extend(results)
        total_pages = data.get('total_pages', 1)
        logging.debug(f"[Scrob] Collection page {page}/{total_pages} ({len(results)} items)")
        if page >= total_pages:
            break
        page += 1

    processed_items = process_scrob_items(all_items, unblacklist=unblacklist)
    logging.info(f"Found {len(processed_items)} items from Scrob collection")
    all_wanted_items.append((processed_items, versions))
    return all_wanted_items


def get_wanted_from_scrob_special(source_config: Dict[str, Any], versions_profile: Dict[str, Any], unblacklist: bool = False) -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    """Fetches items from configured Scrob special lists (named categories
    and/or genre filters), mirroring get_wanted_from_special_trakt_lists.
    """
    selected_list_types = source_config.get('special_list_type', [])
    selected_genres = source_config.get('special_list_genres', [])
    media_type_filter = (source_config.get('media_type') or 'All').lower()  # movies, shows, all

    if not selected_list_types and not selected_genres:
        logging.warning(f"No special list types or genres selected for source: {source_config.get('display_name')}")
        return []

    all_items_for_this_source = []
    seen_imdb_ids_for_this_source = set()

    def _collect(endpoint: str, params: Optional[Dict[str, Any]] = None):
        data = _scrob_get(endpoint, params=params)
        if not data:
            logging.error(f"Failed to fetch data from Scrob endpoint: {endpoint} (params={params})")
            return
        raw_items = data.get('results', [])
        if not isinstance(raw_items, list):
            logging.warning(f"Expected a list of results from {endpoint}, got {type(raw_items)}. Skipping.")
            return
        for item_detail in process_scrob_items(raw_items, unblacklist=unblacklist):
            if item_detail['imdb_id'] not in seen_imdb_ids_for_this_source:
                all_items_for_this_source.append(item_detail)
                seen_imdb_ids_for_this_source.add(item_detail['imdb_id'])

    for list_type in selected_list_types:
        if list_type not in SPECIAL_LIST_ENDPOINTS:
            logging.warning(f"Unknown Scrob special list type '{list_type}' in source config. Skipping.")
            continue

        api_paths_for_type = SPECIAL_LIST_ENDPOINTS[list_type]
        endpoints_to_call = []
        if media_type_filter in ('movies', 'all') and api_paths_for_type.get("movies"):
            endpoints_to_call.append(api_paths_for_type["movies"])
        if media_type_filter in ('shows', 'all') and api_paths_for_type.get("shows"):
            endpoints_to_call.append(api_paths_for_type["shows"])

        for endpoint_path, endpoint_params in endpoints_to_call:
            logging.info(f"Fetching from Scrob special list '{list_type}', endpoint: {endpoint_path} (params={endpoint_params})")
            _collect(endpoint_path, params=endpoint_params)

    for genre in selected_genres:
        if media_type_filter in ('movies', 'all'):
            logging.info(f"Fetching Scrob genre discover: movies / {genre}")
            _collect("/media/tmdb/list", params={'type': 'movie', 'genre': genre})
        if media_type_filter in ('shows', 'all'):
            logging.info(f"Fetching Scrob genre discover: shows / {genre}")
            _collect("/media/tmdb/list", params={'type': 'series', 'genre': genre})

    if not all_items_for_this_source:
        logging.info(f"No items found for Scrob special list source: {source_config.get('display_name')}")
        return []

    logging.info(f"Found {len(all_items_for_this_source)} unique items from Scrob special list source: {source_config.get('display_name')}")
    return [(all_items_for_this_source, versions_profile)]


# ── Deletion sync (write operations — require username/password, not the API key) ──

def is_deletion_sync_configured() -> bool:
    """True if username+password are set, meaning deletion-sync can attempt a login."""
    username = (get_setting('Scrob', 'username', '') or '').strip()
    password = get_setting('Scrob', 'password', '') or ''
    return bool(username and password)


def remove_from_scrob_list(list_id: Any, items: list) -> dict:
    """Removes items from a single Scrob list by tmdb_id/type match.

    Args:
        list_id: Numeric Scrob list ID (from the source's scrob_list_ids field)
        items: List of item dicts, each with 'tmdb_id' and 'type'

    Returns:
        dict: {'success': bool, 'removed': int, 'message': str}
    """
    if not is_deletion_sync_configured():
        return {'success': False, 'removed': 0, 'message': 'Scrob username/password not configured — deletion sync skipped'}

    list_data = _scrob_get(f"/lists/{list_id}")
    if not list_data:
        return {'success': False, 'removed': 0, 'message': f'Could not fetch Scrob list {list_id}'}

    # Build tmdb_id -> list-item-id lookup for this list's current contents.
    by_tmdb: Dict[str, int] = {}
    for list_item in list_data.get('items', []):
        media = list_item.get('media', {})
        tmdb_id = media.get('tmdb_id')
        if tmdb_id:
            by_tmdb[str(tmdb_id)] = list_item['id']

    removed = 0
    not_found = []
    for item in items:
        tmdb_id = item.get('tmdb_id')
        item_title = item.get('title', 'Unknown')
        if not tmdb_id or str(tmdb_id) not in by_tmdb:
            not_found.append(item_title)
            continue
        list_item_id = by_tmdb[str(tmdb_id)]
        result = _scrob_write('delete', f"/lists/{list_id}/items/{list_item_id}")
        if result is not None:
            removed += 1
            logging.info(f"[SCROB_LIST] Removed '{item_title}' (tmdb_id={tmdb_id}) from list {list_id}")
        else:
            not_found.append(item_title)
            logging.warning(f"[SCROB_LIST] Failed to remove '{item_title}' (tmdb_id={tmdb_id}) from list {list_id}")

    if not_found:
        logging.info(f"[SCROB_LIST] Not removed from list {list_id}: {not_found}")

    return {
        'success': removed > 0,
        'removed': removed,
        'message': f"Removed {removed} item(s) from Scrob list {list_id}" + (f", {len(not_found)} not found/failed" if not_found else "")
    }


def remove_from_scrob_collection(items: list) -> dict:
    """Removes items from the user's Scrob collection.

    Args:
        items: List of item dicts, each with 'tmdb_id' and 'type'

    Returns:
        dict: {'success': bool, 'removed': int, 'message': str}
    """
    if not is_deletion_sync_configured():
        return {'success': False, 'removed': 0, 'message': 'Scrob username/password not configured — deletion sync skipped'}

    removed = 0
    not_found = []
    for item in items:
        tmdb_id = item.get('tmdb_id')
        item_title = item.get('title', 'Unknown')
        raw_type = (item.get('type') or '').lower()
        scrob_media_type = 'movie' if raw_type == 'movie' else 'series'

        if not tmdb_id:
            not_found.append(item_title)
            continue

        result = _scrob_write('delete', "/media/collect", params={'tmdb_id': tmdb_id, 'media_type': scrob_media_type})
        if result is not None:
            removed += 1
            logging.info(f"[SCROB_COLLECTION] Removed '{item_title}' (tmdb_id={tmdb_id}) from collection")
        else:
            not_found.append(item_title)
            logging.warning(f"[SCROB_COLLECTION] Failed to remove '{item_title}' (tmdb_id={tmdb_id}) from collection")

    if not_found:
        logging.info(f"[SCROB_COLLECTION] Not removed: {not_found}")

    return {
        'success': removed > 0,
        'removed': removed,
        'message': f"Removed {removed} item(s) from Scrob collection" + (f", {len(not_found)} not found/failed" if not_found else "")
    }
