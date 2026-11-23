"""Magnet blueprint, assignment store, season cache and episode-order helpers shared by the magnet route modules."""

from flask import Blueprint, jsonify
import threading as _threading
from queues.config_manager import load_config
import logging
import os
import requests
from content_checkers.trakt import get_trakt_headers, TRAKT_API_URL, REQUEST_TIMEOUT
import asyncio
from utilities.web_scraper import get_media_meta, SearchCache
from utilities.settings import get_setting
from typing import List, Dict, Optional, Any

_assignment_store = {}  # token -> assignment data, avoids session size/race issues

_assignment_store_lock = _threading.Lock()

magnet_bp = Blueprint('magnet', __name__)

# PHASE 1.1 & 3.1: Initialize cache for season/episode data
# 60-minute TTL, max 500 entries, with optional Redis support
_season_cache = SearchCache(
    default_ttl_minutes=60,
    max_size=500,
    redis_url=get_setting('Redis', 'url', None) or os.environ.get('REDIS_URL')
)

async def _fetch_media_details_for_assigner(id_value: str, id_kind: str, content_type_hint: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Helper to fetch media details based on IMDb or TMDb ID for magnet assigner.
    Returns a list containing a single result dict if found, else empty list.
    """
    from metadata.metadata import get_metadata
    tmdb_api_key = get_setting('TMDB', 'api_key')
    has_tmdb = bool(tmdb_api_key)
    results = []
    metadata_result = None
    determined_media_type = None # Will be 'movie' or 'tv'

    try:
        if id_kind == 'imdb':
            # Use hint if provided, otherwise try movie first then tv
            first_type = 'tv' if content_type_hint in ('tv', 'show') else 'movie'
            second_type = 'movie' if first_type == 'tv' else 'tv'
            metadata_result = get_metadata(imdb_id=id_value, item_media_type=first_type)
            if metadata_result and metadata_result.get('tmdb_id'):
                determined_media_type = first_type
            else:
                metadata_result = get_metadata(imdb_id=id_value, item_media_type=second_type)
                if metadata_result and metadata_result.get('tmdb_id'):
                    determined_media_type = second_type
        elif id_kind == 'tmdb':
            try:
                tmdb_id_int = int(id_value)
                if content_type_hint == 'movie':
                    metadata_result = get_metadata(tmdb_id=tmdb_id_int, item_media_type='movie')
                    if metadata_result and metadata_result.get('tmdb_id'):
                        determined_media_type = 'movie'
                elif content_type_hint == 'show' or content_type_hint == 'tv':
                    metadata_result = get_metadata(tmdb_id=tmdb_id_int, item_media_type='tv')
                    if metadata_result and metadata_result.get('tmdb_id'):
                        determined_media_type = 'tv'
                else: # No hint, try movie then tv
                    metadata_result = get_metadata(tmdb_id=tmdb_id_int, item_media_type='movie')
                    if metadata_result and metadata_result.get('tmdb_id'):
                        determined_media_type = 'movie'
                    else:
                        metadata_result = get_metadata(tmdb_id=tmdb_id_int, item_media_type='tv')
                        if metadata_result and metadata_result.get('tmdb_id'):
                            determined_media_type = 'tv'
                
                # If get_metadata failed (returned empty dict), try with dummy IMDb ID
                if not metadata_result or not metadata_result.get('tmdb_id'):
                    logging.warning(f"Could not get metadata for TMDB ID {tmdb_id_int}. Trying with dummy IMDb ID...")
                    # Generate a dummy IMDb ID and try to get basic metadata
                    dummy_imdb_id = f"tt{tmdb_id_int:07d}"
                    logging.info(f"Generated dummy IMDb ID: {dummy_imdb_id} for TMDB ID {tmdb_id_int}")
                    
                    # Try to get basic TMDB metadata using get_media_meta
                    try:
                        media_type_for_meta = content_type_hint or 'movie'
                        media_meta_tuple = await asyncio.to_thread(
                            get_media_meta, str(tmdb_id_int), media_type_for_meta
                        )
                        
                        if media_meta_tuple:
                            poster_path, overview, genres, vote_average, backdrop_path = media_meta_tuple
                            # Create minimal metadata with dummy IMDb ID
                            metadata_result = {
                                'tmdb_id': tmdb_id_int,
                                'imdb_id': dummy_imdb_id,
                                'title': f'TMDB {tmdb_id_int}',  # Fallback title
                                'year': None,
                                'genres': genres or [],
                                'overview': overview or '',
                                'poster': poster_path
                            }
                            determined_media_type = 'movie' if media_type_for_meta == 'movie' else 'tv'
                            logging.info(f"Created minimal metadata with dummy IMDb ID {dummy_imdb_id} for TMDB ID {tmdb_id_int}")
                        else:
                            logging.warning(f"Could not get media meta for TMDB ID {tmdb_id_int} with dummy IMDb ID")
                    except Exception as meta_error:
                        logging.error(f"Error getting media meta for TMDB ID {tmdb_id_int}: {meta_error}")
                        
            except ValueError:
                logging.error(f"Invalid TMDb ID format: {id_value}")
                return []

        if metadata_result and determined_media_type and metadata_result.get('tmdb_id'):
            tmdb_id_from_meta = str(metadata_result.get('tmdb_id'))
            title = metadata_result.get('title', 'N/A')
            year = metadata_result.get('year', 'N/A')
            
            poster_path_final = None
            
            # Use get_media_meta for consistent poster/details if possible
            # get_media_meta expects determined_media_type as 'movie' or 'tv'
            media_meta_tuple = await asyncio.to_thread(
                get_media_meta, tmdb_id_from_meta, determined_media_type
            )

            backdrop_path_final = None
            if media_meta_tuple:
                poster_path_from_meta, _, _, _, backdrop_path_from_meta = media_meta_tuple
                poster_path_final = poster_path_from_meta # This is a relative TMDB path
                if backdrop_path_from_meta and has_tmdb:
                    backdrop_path_final = f"/scraper/tmdb_image/w780{backdrop_path_from_meta}"

            # Construct full poster path using proxy or use placeholder
            if poster_path_final and poster_path_final != "static/images/placeholder.png":
                if has_tmdb:
                    # Use the scraper's TMDB image proxy if TMDB key is set
                    poster_path_final = f"/scraper/tmdb_image/w300{poster_path_final}"
                else:
                    # Fallback if TMDB key is not set but we got a path (should ideally be placeholder already)
                    poster_path_final = "static/images/placeholder.png"
            elif metadata_result.get('poster') and has_tmdb : # Fallback to poster from get_metadata
                 poster_path_final = f"/scraper/tmdb_image/w300{metadata_result.get('poster')}"
            else: # Ultimate fallback to placeholder
                poster_path_final = "static/images/placeholder.png"


            formatted_result = {
                'id': tmdb_id_from_meta,
                'title': title,
                'year': year,
                'posterPath': poster_path_final,
                'backdropPath': backdrop_path_final,
                'mediaType': 'show' if determined_media_type == 'tv' else 'movie'
            }
            results.append(formatted_result)
        else:
            logging.warning(f"Could not find metadata for {id_kind} ID: {id_value} with hint {content_type_hint}")

    except Exception as e:
        logging.error(f"Error during _fetch_media_details_for_assigner ({id_kind}={id_value}): {e}", exc_info=True)
    return results

def _fetch_trakt_season_data_directly(imdb_id: str) -> dict | None:
    """Fetch season episode counts directly from Trakt API, including Season 0."""
    headers = get_trakt_headers()
    if not headers:
        logging.error("Failed to get Trakt headers for direct season fetch.")
        return None
        
    # Use extended=episodes, we only need the counts
    url = f"{TRAKT_API_URL}/shows/{imdb_id}/seasons?extended=episodes" 
    logging.debug(f"Fetching season data directly from Trakt: {url}")
    
    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()  # Raise HTTPError for bad responses (4xx or 5xx)
        
        seasons_data = response.json()
        if not isinstance(seasons_data, list):
             logging.error(f"Unexpected response format from Trakt seasons endpoint: {type(seasons_data)}")
             return None

        # Process seasons, including season 0
        season_counts = {}
        for season in seasons_data:
            season_num = season.get('number')
            # Ensure season number is not None before processing
            if season_num is not None:
                # Use episode_count if available, otherwise count episodes list
                episode_count = season.get('episode_count', len(season.get('episodes', [])))
                season_counts[season_num] = episode_count
            else:
                logging.warning(f"Skipping season with null number for IMDb ID {imdb_id}")

        # Convert to the expected format (list of dictionaries)
        formatted_seasons = []
        for season_num, episode_count in season_counts.items():
            formatted_seasons.append({
                'number': season_num,
                'episode_count': episode_count
            })
        
        logging.debug(f"Directly fetched season counts for {imdb_id}: {season_counts}")
        return formatted_seasons

    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching season data directly from Trakt for {imdb_id}: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logging.error(f"Trakt API response status: {e.response.status_code}")
            logging.error(f"Trakt API response text: {e.response.text}")
        return None
    except Exception as e:
        logging.error(f"Unexpected error fetching season data directly from Trakt: {e}", exc_info=True)
        return None

@magnet_bp.route('/get_versions')
def get_versions():
    settings = load_config()
    version_terms = settings.get('Scraping', {}).get('versions', {})
    # Return list of version keys
    return jsonify(list(version_terms.keys()))

def _requested_episode_order(raw):
    """A valid episode order id from the form, or None."""
    raw = (raw or '').strip()
    if raw == 'default' or raw.startswith('tvdb:') or raw.startswith('tmdb_group:'):
        return raw
    return None

def _saved_episode_order(imdb_id):
    from cli_battery.app.episode_order import show_episode_order
    return show_episode_order(imdb_id) or 'default'

def _library_episode_rows(imdb_id):
    from database import get_db_connection
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT COUNT(*) FROM media_items WHERE imdb_id = ? AND type = 'episode'",
                           (imdb_id,)).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()

def _order_change_blocked(imdb_id, requested_order):
    """Why `requested_order` can't be set from here, or None.

    A show already in the library has to switch order from its show page,
    which moves and renames the existing episodes; here it can only be set
    before anything is added.
    """
    if not requested_order or requested_order == _saved_episode_order(imdb_id):
        return None
    rows = _library_episode_rows(imdb_id)
    if rows:
        return (f'This show already has {rows} episode(s) in the library in another episode order. '
                f'Change its order from the show page (Show options) first, then assign.')
    return None

def _mark_items_in_library(target_items, imdb_id, version):
    """Flag items already Collected in this version (in_library), which the
    "Replace episodes already in the library" option would replace."""
    if not imdb_id or not target_items:
        return
    version = (version or '').rstrip('*')
    try:
        from database import get_db_connection
        conn = get_db_connection()
        try:
            rows = conn.execute(
                "SELECT type, season_number, episode_number FROM media_items WHERE imdb_id = ? "
                "AND state = 'Collected' AND (ghostlisted = 0 OR ghostlisted IS NULL) "
                "AND RTRIM(COALESCE(version, ''), '*') = ?", (imdb_id, version)).fetchall()
        finally:
            conn.close()
    except Exception as e:
        logging.warning(f"Could not read library items for {imdb_id}: {e}")
        return
    have = {(r['type'], r['season_number'] if r['type'] == 'episode' else None,
             r['episode_number'] if r['type'] == 'episode' else None) for r in rows}
    for item in target_items:
        episode = item.get('type') == 'episode'
        item['in_library'] = (item.get('type'), item.get('season_number') if episode else None,
                              item.get('episode_number') if episode else None) in have

def _seasons_for_order(imdb_id, order):
    """Seasons/episodes (specials included) in `order`; None if unavailable."""
    from cli_battery.app.episode_order import fetch_seasons_for_order
    try:
        return fetch_seasons_for_order(imdb_id, order)
    except Exception as e:
        logging.error(f"Fetching episode order {order} for {imdb_id} failed: {e}", exc_info=True)
        return None

def _save_episode_order(imdb_id, tmdb_id, order):
    """Set the order on a show that has no library episodes yet."""
    from database.show_overrides import set_show_override
    from utilities.episode_order_change import _reset_battery_seasons
    set_show_override(imdb_id, tmdb_id=tmdb_id, episode_order=None if order == 'default' else order)
    _reset_battery_seasons(imdb_id)
    _season_cache.clear()  # season counts cached in the old order
    logging.info(f"Manual assign: episode order for {imdb_id} set to {order}")

def _sync_plex_order_for_new_show(imdb_id, order):
    """Set Plex's ordering for a show whose order was just picked here. The
    show is usually not in Plex yet, so this mostly returns a reminder."""
    try:
        from utilities.episode_order_change import _plex_ordering_for, sync_plex_ordering
        from utilities.plex_order_metadata import _nfo_mode, sync_show
        candidates = _plex_ordering_for(order)
        if _nfo_mode(imdb_id):
            # NFO files carry the order; sync_show numbers the show by TMDB in
            # Plex once it's there (and new episodes do it when Plex adds them)
            titles = sync_show(imdb_id, order)
            return titles or "NFO files carry this order; Plex follows them once it has the show."
        note = (sync_plex_ordering(imdb_id, candidates, []) if candidates
                else "Plex has no matching episode ordering")
        if note.startswith('Show not found in Plex'):
            return "Once the show appears in Plex, set its episode ordering there to match."
        # Already in Plex: release title locks left by older versions
        titles = sync_show(imdb_id, order)
        return f"{note}; {titles}" if titles else note
    except Exception as e:
        logging.warning(f"Plex ordering for {imdb_id} after manual assign failed: {e}")
        return None
