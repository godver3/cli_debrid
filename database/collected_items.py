from .core import get_db_connection, row_to_dict, normalize_string, get_existing_airtime
import logging
import time as _time

# Suppress repeated "unmatched Plex item" warnings for the same filename (1 hour)
_unmatched_warned: dict = {}
_UNMATCHED_SUPPRESS_SECS = 3600

# Serialize concurrent calls to add_collected_items — prevents "database is locked"
# when multiple tasks (reconciliation, Plex check, content source) call simultaneously
import threading
_add_collected_lock = threading.Lock()
import os
from datetime import datetime, timezone, timedelta
import json
from .database_writing import add_to_collected_notifications, update_media_item_state, RESET_COLLECTION_STATE_SQL
from utilities.reverse_parser import parser_approximation
from utilities.settings import get_setting
from typing import Dict, Any, List, Optional
from utilities.post_processing import handle_state_change
from cli_battery.app.direct_api import DirectAPI
import sqlite3
import time

def _cache_tmdb_artwork(media_items_batch):
    """
    Cache poster and backdrop URLs from TMDB.
    Only caches items that are not already in cache or have expired.
    """
    from routes.poster_cache import load_cache, save_cache, normalize_media_type, CACHE_EXPIRY_DAYS
    from utilities.web_scraper import get_media_meta

    # Load cache once for the entire batch
    cache = load_cache()
    cache_updated = False
    cached_count = 0
    skipped_count = 0

    for item in media_items_batch:
        tmdb_id = item.get('tmdb_id')
        if not tmdb_id:
            continue

        # Determine media type for cache
        item_type = item.get('type', 'movie')
        if item_type == 'episode':
            media_type = 'tv'
        else:
            media_type = 'movie'

        normalized_type = normalize_media_type(media_type)

        # Check and cache poster
        poster_key = f"{tmdb_id}_{normalized_type}"

        # Check if already cached and not expired
        cache_item = cache.get(poster_key)
        if cache_item:
            url, timestamp = cache_item
            if datetime.now() - timestamp < timedelta(days=CACHE_EXPIRY_DAYS):
                skipped_count += 1
            else:
                # Expired, update from TMDB
                try:
                    media_meta = get_media_meta(str(tmdb_id), media_type)
                    if media_meta and media_meta[0]:
                        cache[poster_key] = (media_meta[0], datetime.now())
                        cache_updated = True
                        cached_count += 1
                except Exception as e:
                    logging.debug(f"TMDB failed for poster {tmdb_id}: {e}")
        else:
            # Not in cache, fetch from TMDB
            try:
                media_meta = get_media_meta(str(tmdb_id), media_type)
                if media_meta and media_meta[0]:
                    cache[poster_key] = (media_meta[0], datetime.now())
                    cache_updated = True
                    cached_count += 1
            except Exception as e:
                logging.debug(f"TMDB failed for poster {tmdb_id}: {e}")

        # Check and cache backdrop
        backdrop_key = f"{tmdb_id}_backdrop_{normalized_type}"

        # Check if already cached and not expired
        cache_item = cache.get(backdrop_key)
        if cache_item:
            url, timestamp = cache_item
            if datetime.now() - timestamp < timedelta(days=CACHE_EXPIRY_DAYS):
                skipped_count += 1
            else:
                # Expired, update from TMDB
                try:
                    media_meta = get_media_meta(str(tmdb_id), media_type)
                    if media_meta and media_meta[4]:
                        cache[backdrop_key] = (media_meta[4], datetime.now())
                        cache_updated = True
                        cached_count += 1
                except Exception as e:
                    logging.debug(f"TMDB failed for backdrop {tmdb_id}: {e}")
        else:
            # Not in cache, fetch from TMDB
            try:
                media_meta = get_media_meta(str(tmdb_id), media_type)
                if media_meta and media_meta[4]:
                    cache[backdrop_key] = (media_meta[4], datetime.now())
                    cache_updated = True
                    cached_count += 1
            except Exception as e:
                logging.debug(f"TMDB failed for backdrop {tmdb_id}: {e}")

    # Save cache only once if any updates were made
    if cache_updated:
        save_cache(cache)
        logging.info(f"[Artwork Cache - TMDB] Cached {cached_count} new/expired items, skipped {skipped_count} already cached")
    else:
        logging.debug(f"[Artwork Cache] All {skipped_count} items already cached, no updates needed")

def add_collected_items(media_items_batch, recent=False, backfill=False, data_source='plex'):
    """
    Add or update collected media items in the database.

    Args:
        media_items_batch: List of media items to add/update
        recent: True when adding new items from Plex/Zurg
        backfill: True when updating file sizes/metadata for existing items
        data_source: Source of the data ('plex' or 'filesystem')
    """
    with _add_collected_lock:
        return _add_collected_items_impl(media_items_batch, recent=recent, backfill=backfill, data_source=data_source)


def _add_collected_items_impl(media_items_batch, recent=False, backfill=False, data_source='plex'):
    from datetime import datetime, timedelta
    from utilities.settings import get_setting
    from queues.upgrading_queue import log_successful_upgrade
    from metadata.metadata import get_show_airtime_by_imdb_id

    # Check if watch history filtering is enabled
    do_not_add_watched = get_setting('Debug', 'do_not_add_plex_watch_history_items_to_queue', False)
    watch_history_conn = None
    if do_not_add_watched:
        db_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        watch_db_path = os.path.join(db_dir, 'watch_history.db')
        if os.path.exists(watch_db_path):
            watch_history_conn = get_db_connection(watch_db_path)
            logging.debug("Watch history filtering enabled for collected items")

    # Check if Plex library checks are disabled
    if get_setting('Plex', 'disable_plex_library_checks', default=False):
        logging.info("Plex library checks disabled - using simplified collection process")
        return plex_collection_disabled(media_items_batch)

    # Cache posters and backdrops from TMDB
    # Skip during backfill - existing items already have cached artwork
    if not backfill:
        _cache_tmdb_artwork(media_items_batch)
        logging.info(f"Cached TMDB artwork for {len(media_items_batch)} items")
    else:
        logging.info(f"[BACKFILL] Skipping artwork caching for {len(media_items_batch)} existing items (performance optimization)")

    conn = get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')

        # Collect items that need post-processing after transaction commits
        items_for_post_processing = []

        existing_collected_files = set()
        upgrading_from_files = set()
        existing_file_map = {}

        # Collect filenames from media_items_batch to limit our queries
        filenames_in_batch = set()
        for item in media_items_batch:
            locations = item.get('location', [])
            if isinstance(locations, str):
                locations = [locations]
            for location in locations:
                filename = os.path.basename(location)
                if filename:
                    filenames_in_batch.add(filename)
        
        if filenames_in_batch:
            # Process filenames in batches to avoid SQLite variable limit
            batch_size = 450
            filenames_list = list(filenames_in_batch)
            existing_items = []
            
            for i in range(0, len(filenames_list), batch_size):
                batch = filenames_list[i:i + batch_size]
                placeholders = ', '.join(['?'] * len(batch))
                
                query = f'''
                    SELECT id, imdb_id, tmdb_id, title, type, season_number, episode_number, state, version,
                           filled_by_file, collected_at, release_date, upgrading_from, content_source,
                           location_on_disk, location_basename, ghostlisted,
                           upgrading_from_torrent_id, resolution
                    FROM media_items
                    WHERE filled_by_file IN ({placeholders})
                       OR upgrading_from IN ({placeholders})
                       OR location_basename IN ({placeholders})
                       OR location_on_disk IN ({placeholders})
                '''
                
                params = batch * 4
                cursor = conn.execute(query, params)
                existing_items.extend(cursor.fetchall())
                cursor.close()
            
            # Process the results
            for row in existing_items:
                filled_by_file = row['filled_by_file']
                upgrading_from = os.path.basename(row['upgrading_from'] or '')
                location_basename = row['location_basename']
                location_on_disk_basename = os.path.basename(row['location_on_disk'] or '')
                location_on_disk_full = row['location_on_disk'] or ''  # Keep full path for symlink mode matching
                state = row['state']

                if state == 'Collected':
                    if filled_by_file: existing_collected_files.add(filled_by_file)
                    if location_basename: existing_collected_files.add(location_basename)
                    if location_on_disk_basename: existing_collected_files.add(location_on_disk_basename)
                    if location_on_disk_full: existing_collected_files.add(location_on_disk_full)  # Add full path too
                if state == 'Upgrading':
                    if filled_by_file:
                        existing_collected_files.add(filled_by_file)
                    if upgrading_from:
                        upgrading_from_files.add(upgrading_from)

                dict_row = row_to_dict(row)
                if filled_by_file:
                    existing_file_map[filled_by_file] = dict_row
                if upgrading_from:
                    existing_file_map[upgrading_from] = dict_row
                if location_basename:
                    existing_file_map[location_basename] = dict_row
                if location_on_disk_basename:
                    existing_file_map[location_on_disk_basename] = dict_row
                # IMPORTANT: Also add full path for symlink mode - allows different versions
                # with same filename in different folders to be tracked separately
                if location_on_disk_full:
                    existing_file_map[location_on_disk_full] = dict_row

        # For NZB items in Checking/Adding state, the filename in DB may be a UUID
        # that doesn't match the real Plex filename. Build an imdb-keyed set of
        # in-flight items so Plex scan doesn't insert duplicates for them.
        if recent:
            _inflight_keys = set()  # (imdb_id, type, season, episode) for items in Checking/Adding
            _batch_imdb_ids = {item.get('imdb_id') for item in media_items_batch if item.get('imdb_id')}
            if _batch_imdb_ids:
                _ph = ','.join('?' * len(_batch_imdb_ids))
                _inflight_rows = conn.execute(
                    f"SELECT imdb_id, type, season_number, episode_number, version, state FROM media_items "
                    f"WHERE imdb_id IN ({_ph}) AND state IN ('Checking','Adding','Collected','Upgrading')",
                    list(_batch_imdb_ids)
                ).fetchall()
                for _r in _inflight_rows:
                    if _r['state'] in ('Checking', 'Adding', 'Upgrading'):
                        # Block same episode regardless of version — it's actively being processed
                        _inflight_keys.add((_r['imdb_id'], _r['type'], _r['season_number'], _r['episode_number'], None))
                    else:
                        # Collected: block same episode+version combo to prevent duplicates
                        # but allow different versions (legitimate multi-version collection)
                        _ver = (_r['version'] or '').rstrip('*')  # strip asterisk for comparison
                        _inflight_keys.add((_r['imdb_id'], _r['type'], _r['season_number'], _r['episode_number'], _ver))
        else:
            _inflight_keys = set()

        filtered_out_files = set()
        filtered_media_items_batch = []
        for item in media_items_batch:
            locations = item.get('location', [])
            if isinstance(locations, str):
                locations = [locations]

            new_locations = []
            for location in locations:
                filename = os.path.basename(location)
                if recent:
                    # Check both full path and filename - full path takes precedence (symlink mode)
                    # This allows different versions with same filename but different paths to be tracked
                    is_full_path_collected = location in existing_collected_files
                    is_filename_collected = filename and filename in existing_collected_files
                    is_upgrading_from = filename and filename in upgrading_from_files

                    # If filename is collected but the full path differs (or location_on_disk is NULL),
                    # this may be a stale/missing location_on_disk from an upgrade that completed
                    # without updating the path (e.g. task_check_plex_files tick/time fallback).
                    # Allow it through so the else branch in the processing loop can update location_on_disk.
                    if is_filename_collected and not is_full_path_collected:
                        db_item = existing_file_map.get(filename)
                        if db_item:
                            db_location = db_item.get('location_on_disk') or ''
                            if not db_location or db_location != location:
                                logging.debug(f"[Collection Filter] Allowing '{location}' - location_on_disk missing or mismatch (upgrade stale path), current='{db_location}'")
                                is_filename_collected = False

                    # Check if in-flight (Checking/Adding) by imdb_id to catch NzbDAV UUID filenames
                    _item_imdb = item.get('imdb_id')
                    _item_type = item.get('type')
                    _item_season = item.get('season_number')
                    _item_episode = item.get('episode_number')
                    is_inflight = bool(_item_imdb and (_item_imdb, _item_type, _item_season, _item_episode) in _inflight_keys)

                    # If full path is NOT collected AND (filename is NOT collected OR filename not set)
                    # then include this location
                    if not is_full_path_collected and not is_filename_collected and not is_upgrading_from and not is_inflight:
                        new_locations.append(location)
                    else:
                        filtered_out_files.add(location)  # Track full path, not just filename
                        logging.debug(f"[Collection Filter] Skipping '{location}' - full_path_collected={is_full_path_collected}, filename_collected={is_filename_collected}, upgrading_from={is_upgrading_from}, inflight={is_inflight}")
                else:
                    new_locations.append(location)

            if new_locations:
                item['location'] = new_locations
                filtered_media_items_batch.append(item)

        all_valid_filenames = set()
        airtime_cache = {}
        
        # --- Pre-fetch Airtime Logic ---
        new_episode_show_ids = set()

        # Identify unique show IMDb IDs for *new* episodes in the filtered batch
        for item in filtered_media_items_batch:
            if item.get('type') == 'episode' and item.get('imdb_id'):
                locations = item.get('location', [])
                if isinstance(locations, str): locations = [locations]
                is_new = True
                for loc in locations:
                    fname = os.path.basename(loc)
                    # Check against the map of known files (more accurate than just collected files)
                    if fname in existing_file_map:
                        is_new = False
                        break
                if is_new:
                    new_episode_show_ids.add(item['imdb_id'])

        if new_episode_show_ids:
            # logging.info(f"Found {len(new_episode_show_ids)} unique show IDs potentially requiring airtime check for new episodes.")

            # 1. Bulk check media_items DB for existing airtimes
            ids_to_check_list = list(new_episode_show_ids)
            batch_size_db = 900 # SQLite parameter limit / 2
            try:
                for i in range(0, len(ids_to_check_list), batch_size_db):
                    batch_ids = ids_to_check_list[i:i+batch_size_db]
                    if not batch_ids: continue # Skip empty batch
                    placeholders = ','.join('?' * len(batch_ids))
                    query = f"SELECT imdb_id, airtime FROM media_items WHERE imdb_id IN ({placeholders}) AND airtime IS NOT NULL GROUP BY imdb_id"
                    cursor = conn.execute(query, batch_ids)
                    for row in cursor:
                        if row['imdb_id'] and row['airtime']:
                            airtime_cache[row['imdb_id']] = row['airtime']
                    cursor.close()
            except Exception as db_err:
                 logging.error(f"Error querying existing airtimes from media_items: {db_err}")

            # logging.info(f"Found {len(airtime_cache)} existing airtimes in media_items DB.")

            # 2. Identify shows still needing airtime check via battery metadata
            ids_needing_metadata_check = list(new_episode_show_ids - set(airtime_cache.keys()))

            if ids_needing_metadata_check:
                # logging.info(f"Checking battery metadata for 'airs' info for {len(ids_needing_metadata_check)} show IDs.")
                try:
                    # Bulk query battery for 'airs' info
                    bulk_airs_info = DirectAPI.get_bulk_show_airs(ids_needing_metadata_check)

                    # Populate cache from the bulk result
                    for imdb_id, airs_data in bulk_airs_info.items():
                        # Ensure we don't overwrite if already found in media_items
                        if imdb_id not in airtime_cache:
                            if airs_data and isinstance(airs_data, dict) and 'time' in airs_data:
                                airtime_value = airs_data['time']
                                # Basic format check (HH:MM or HH:MM:SS) and ensure not None/empty
                                if isinstance(airtime_value, str) and airtime_value and ':' in airtime_value:
                                    airtime_cache[imdb_id] = airtime_value[:5] # Store as HH:MM
                                else:
                                    logging.warning(f"Invalid or missing airtime format ('{airtime_value}') in metadata for {imdb_id}. Using default.")
                                    airtime_cache[imdb_id] = '19:00' # Default if format invalid
                            else:
                                # If airs data not found in battery, use default
                                # logging.info(f"No valid 'airs' metadata found in battery for {imdb_id}. Using default airtime.")
                                airtime_cache[imdb_id] = '19:00' # Default if no airs info
                except Exception as bulk_err:
                    logging.error(f"Error during bulk airs metadata check: {bulk_err}. Using default airtime for remaining shows.")
                    # Assign default to remaining IDs on error only if not already cached
                    for imdb_id in ids_needing_metadata_check:
                        if imdb_id not in airtime_cache:
                            airtime_cache[imdb_id] = '19:00'

            # Ensure all initially identified IDs have *some* value in the cache (assign default if missed)
            for imdb_id in new_episode_show_ids:
                if imdb_id not in airtime_cache:
                     logging.warning(f"Show ID {imdb_id} missed airtime assignment, assigning default '19:00'.")
                     airtime_cache[imdb_id] = '19:00'
            # logging.info(f"Airtime cache populated for {len(airtime_cache)} shows.")

        # --- End Pre-fetch Airtime Logic ---

        logging.info(f"[Collection Debug] Starting processing of {len(filtered_media_items_batch)} filtered media items.")
        # start_time_batch = time.time()

        for index, item in enumerate(filtered_media_items_batch):
            item_identifier = generate_identifier(item)
            # start_time_item = time.time()

            plex_locations = item.get('location', [])
            if isinstance(plex_locations, str):
                plex_locations = [plex_locations]

            # Debug: Log each item being processed with its location
            logging.debug(f"[Collection Debug] Processing item {index+1}/{len(filtered_media_items_batch)}: {item_identifier}, locations: {plex_locations}")

            # Enhanced logging: Count existing 'Checking' items for this Plex item's identifiers
            checking_items_count = 0
            checking_item_ids_for_plex_item = []
            if item.get('imdb_id') or item.get('tmdb_id'):
                query_parts = []
                params = []
                if item.get('imdb_id'):
                    query_parts.append("imdb_id = ?")
                    params.append(item.get('imdb_id'))
                if item.get('tmdb_id'):
                    query_parts.append("tmdb_id = ?")
                    params.append(item.get('tmdb_id'))
                
                id_condition = " OR ".join(query_parts)
                
                if item.get('type') == 'episode':
                    query = f"SELECT id FROM media_items WHERE ({id_condition}) AND type = 'episode' AND season_number = ? AND episode_number = ? AND state = 'Checking'"
                    params.extend([item.get('season_number'), item.get('episode_number')])
                else: # movie
                    query = f"SELECT id FROM media_items WHERE ({id_condition}) AND type = 'movie' AND state = 'Checking'"
                
                try:
                    cursor = conn.execute(query, tuple(params))
                    checking_rows = cursor.fetchall()
                    checking_items_count = len(checking_rows)
                    checking_item_ids_for_plex_item = [row['id'] for row in checking_rows]
                    cursor.close()
                except Exception as e_check_query:
                    logging.error(f"Error querying for 'Checking' items for {item_identifier}: {e_check_query}")

            # logging.debug(
            #     f"Processing item {index + 1}/{len(filtered_media_items_batch)}: {item_identifier} "
            #     f"from Plex location(s): {plex_locations}. Found {checking_items_count} matching DB item(s) in 'Checking' state (IDs: {checking_item_ids_for_plex_item})."
            # )

            try:
                # The original 'locations' variable was for Plex item locations.
                # We iterate through these locations to process each file.
                # Renaming to avoid confusion if 'location' is used later for DB item's location.

                for plex_file_location in plex_locations:
                    filename = os.path.basename(plex_file_location)
                    # Track both filename and full path for cleanup logic
                    if plex_file_location not in filtered_out_files:
                        all_valid_filenames.add(filename)
                        all_valid_filenames.add(plex_file_location)  # Also add full path
                        
                imdb_id = item.get('imdb_id') or None
                tmdb_id = item.get('tmdb_id') or None
                normalized_title = normalize_string(item.get('title', 'Unknown'))
                item_type = 'episode' if 'season_number' in item and 'episode_number' in item else 'movie'
                # For episodes, ms_item_id should be the show's ratingKey (grandparentRatingKey),
                # not the individual episode's ratingKey. Working shows have all episodes share
                # the same ms_item_id = show ratingKey, which is what get_show_seasons() expects.
                if item_type == 'episode':
                    plex_ms_item_id = item.get('grandparentRatingKey') or item.get('ratingKey')
                else:
                    plex_ms_item_id = item.get('ratingKey')

                if imdb_id is None and tmdb_id is None:
                    _key = str(plex_locations)
                    _now = _time.time()
                    if _now - _unmatched_warned.get(_key, 0) > _UNMATCHED_SUPPRESS_SECS:
                        logging.warning(f"Skipping unmatched Plex item: {item.get('title', 'Unknown')} from location(s): {plex_locations}")
                        _unmatched_warned[_key] = _now
                        # Attempt Plex GUID fix-match for this unmatched item.
                        # Look up the DB item by filename, then apply the GUID fast-path.
                        _plex_rating_key = str(plex_ms_item_id) if plex_ms_item_id else None
                        if _plex_rating_key and plex_locations:
                            try:
                                _filename = os.path.basename(plex_locations[0])
                                _db_conn = conn
                                _db_row = _db_conn.execute(
                                    "SELECT id, title, year, imdb_id, tmdb_id, type, season_number, episode_number FROM media_items "
                                    "WHERE (filled_by_file LIKE ? OR location_on_disk LIKE ?) "
                                    "AND state IN ('Checking', 'Collected') LIMIT 1",
                                    (f'%{_filename}%', f'%{_filename}%')
                                ).fetchone()
                                if _db_row and _db_row['tmdb_id']:
                                    _imdb_id = _db_row['imdb_id']
                                    # If imdb_id missing, try to resolve via battery tmdb→imdb mapping
                                    if not _imdb_id and _db_row['tmdb_id']:
                                        try:
                                            from cli_battery.app.direct_api import DirectAPI
                                            _resolved, _ = DirectAPI.tmdb_to_imdb(
                                                str(_db_row['tmdb_id']),
                                                media_type='show' if _db_row['type'] == 'episode' else 'movie'
                                            )
                                            if _resolved:
                                                _imdb_id = _resolved
                                        except Exception:
                                            pass
                                    _media_type = 'show' if _db_row['type'] == 'episode' else 'movie'
                                    _season = _db_row['season_number'] if _db_row['type'] == 'episode' else None
                                    _episode = _db_row['episode_number'] if _db_row['type'] == 'episode' else None
                                    logging.info(
                                        f"[PlexGUID] Unmatched Plex item '{item.get('title')}' — "
                                        f"attempting fix-match via GUID for DB item '{_db_row['title']}' "
                                        f"(imdb={_imdb_id}, ratingKey={_plex_rating_key})"
                                    )
                                    import threading as _fmt_thread
                                    def _run_fix_match(_title, _year, _tmdb, _rk, _imdb, _mtype, _s, _ep):
                                        try:
                                            from utilities.plex_matching_functions import force_match_with_tmdb
                                            force_match_with_tmdb(
                                                _title, _year, _tmdb, _rk,
                                                imdb_id=_imdb, media_type=_mtype,
                                                season=_s, episode=_ep,
                                            )
                                        except Exception as _e:
                                            logging.debug(f"[PlexGUID] Background fix-match failed: {_e}")
                                    _fmt_thread.Thread(
                                        target=_run_fix_match,
                                        args=(_db_row['title'],
                                              str(_db_row['year']) if _db_row['year'] else None,
                                              str(_db_row['tmdb_id']),
                                              _plex_rating_key, _imdb_id,
                                              _media_type, _season, _episode),
                                        daemon=True
                                    ).start()
                            except Exception as _guid_fix_err:
                                logging.debug(f"[PlexGUID] Fix-match attempt failed for unmatched item: {_guid_fix_err}")
                    continue

                # Items tagged by plex_functions with their source library.
                # Secondary libraries share physical files with the primary library —
                # don't let them overwrite location_on_disk or ms_item_id on existing rows.
                is_primary_library = item.get('_plex_library_primary', True)

                # Iterate through each file path provided by Plex for this media item
                for current_plex_location in plex_locations:
                    filename = os.path.basename(current_plex_location) # This is the filename from Plex

                    added_at = item.get('addedAt')
                    if added_at is not None:
                        collected_at = datetime.fromtimestamp(added_at)
                    else:
                        collected_at = datetime.now()
                    genres = json.dumps(item.get('genres', []))

                    # Check for existing item - prefer full path match (symlink mode with multiple versions)
                    # then fall back to filename match (backwards compatibility)
                    lookup_key = None
                    if current_plex_location in existing_file_map:
                        lookup_key = current_plex_location
                        logging.debug(f"[Collection Debug] Full path match for '{current_plex_location}'")
                    elif filename in existing_file_map:
                        lookup_key = filename
                        logging.debug(f"[Collection Debug] Filename match for '{filename}'")
                    else:
                        logging.debug(f"[Collection Debug] No match found for '{current_plex_location}' (filename: '{filename}')")

                    if lookup_key is not None:
                        existing_db_item = existing_file_map[lookup_key]
                        db_item_id = existing_db_item['id']
                        logging.debug(f"[Collection Debug] Found existing DB item ID {db_item_id} for lookup_key '{lookup_key}', state: {existing_db_item['state']}")

                        # Skip if this Plex entry is the OLD pre-upgrade file. After an upgrade,
                        # both the old file (upgrading_from) and new file (filled_by_file) can appear
                        # in Plex simultaneously. Processing the old file would set location_on_disk
                        # back to the stale path, undoing the new file's correct update.
                        _upgrading_from = existing_db_item.get('upgrading_from')
                        if _upgrading_from and filename == os.path.basename(_upgrading_from):
                            logging.info(f"[Collection] Skipping stale pre-upgrade Plex entry '{filename}' for DB item {db_item_id} (upgrading_from match, new file is '{existing_db_item.get('filled_by_file')}')")
                            continue

                        is_this_db_item_checking = existing_db_item['state'] == 'Checking'
                        # other_checking_items_exist = checking_items_count > 0 and (not is_this_db_item_checking or checking_items_count > 1)


                        # logging.debug(
                        #     f"Plex item {item_identifier} (location: {current_plex_location}) matches DB item ID {db_item_id} "
                        #     f"(file: {existing_db_item['filled_by_file']}, state: {existing_db_item['state']}). "
                        #     f"Is this DB item in 'Checking': {is_this_db_item_checking}. "
                        #     f"Total 'Checking' items for these identifiers: {checking_items_count} (IDs: {checking_item_ids_for_plex_item}). "
                        #     f"Other 'Checking' items for this media (excluding this specific file match if it was checking): {other_checking_items_exist}."
                        # )
                        
                        if existing_db_item['state'] not in ['Collected', 'Upgrading']:
                            # Skip ghostlisted or blacklisted items - they should not be re-collected from Plex
                            is_ghostlisted = existing_db_item.get('ghostlisted') == 1
                            is_blacklisted = existing_db_item['state'] == 'Blacklisted'

                            if is_ghostlisted or is_blacklisted:
                                logging.info(f"[Collection] Skipping DB item {db_item_id} ({existing_db_item['title']}) - "
                                           f"item is {'ghostlisted' if is_ghostlisted else 'blacklisted'} and should not be collected from Plex")
                                continue

                            if existing_db_item['release_date'] in ['Unknown', 'unknown', 'None', 'none', None, '']:
                                days_since_release = 0
                                # logging.debug(f"Unknown release date for {item_identifier} - treating as new content")
                            else:
                                try:
                                    release_date = datetime.strptime(existing_db_item['release_date'], '%Y-%m-%d').date()
                                    days_since_release = (datetime.now().date() - release_date).days
                                except ValueError:
                                    # logging.debug(f"Invalid release date format: {existing_db_item['release_date']} - treating as new content")
                                    days_since_release = 0

                            # Check if the DB item was manually assigned
                            is_manually_assigned = existing_db_item.get('content_source') == 'Magnet_Assigner'

                            # Determine the new state, preventing upgrade for manual assignments
                            should_upgrade = (days_since_release <= 7 and
                                              get_setting("Scraping", "enable_upgrading", default=False) and
                                              not is_manually_assigned) # Check if NOT manually assigned

                            if should_upgrade:
                                new_state = 'Upgrading'
                            else:
                                new_state = 'Collected'

                            logging.info(f"[Collection] Setting state for DB item {db_item_id} ({existing_db_item['title']}) to {new_state} (manually_assigned={is_manually_assigned}) "
                                         f"based on Plex item {item_identifier} from {current_plex_location}.")


                            # Determine if this collection event represents an upgrade over a *previous* collection
                            # This 'is_upgrade' flag is primarily for cleanup/notification logic, separate from setting the state
                            is_upgrade = existing_db_item.get('collected_at') is not None 

                            if is_upgrade and existing_db_item.get('upgrading_from'):
                                upgrade_item = {
                                    'type': existing_db_item['type'],
                                    'title': existing_db_item['title'],
                                    'imdb_id': existing_db_item['imdb_id'],
                                    'upgrading_from': existing_db_item['upgrading_from'],
                                    # Use upgrading_from_torrent_id (old torrent) not filled_by_torrent_id (new torrent)
                                    'filled_by_torrent_id': existing_db_item.get('upgrading_from_torrent_id'),
                                    'version': existing_db_item['version'],
                                    'season_number': existing_db_item.get('season_number'),
                                    'episode_number': existing_db_item.get('episode_number'),
                                    'filled_by_file': existing_db_item.get('filled_by_file'),
                                    'resolution': existing_db_item.get('resolution'),
                                    # Full path to old file/symlink on disk (used for symlink mode cleanup)
                                    'location_on_disk': existing_db_item.get('location_on_disk'),
                                }

                                if upgrade_item['filled_by_file'] != upgrade_item['upgrading_from']:
                                    if get_setting("Scraping", "enable_upgrading_cleanup", default=False):
                                        conn.execute('''
                                            UPDATE media_items
                                            SET upgraded = 1
                                            WHERE id = ?
                                        ''', (db_item_id,))

                                        remove_original_item_from_plex(upgrade_item)
                                        remove_original_item_from_account(upgrade_item)
                                        remove_original_item_from_results(upgrade_item, media_items_batch)

                                    # Log success regardless of cleanup setting
                                    log_successful_upgrade(upgrade_item)
                                
                            existing_collected_at = existing_db_item.get('collected_at') or collected_at

                            # Health page replacement cleanup: if the file location is changing
                            # (new replacement file vs old bad file), remove the old Plex entry.
                            # This is separate from the upgrade path — no upgrading_from needed.
                            _old_location = existing_db_item.get('location_on_disk') or ''
                            _new_location_check = current_plex_location or ''
                            if (is_primary_library
                                    and _old_location
                                    and _new_location_check
                                    and _old_location != _new_location_check
                                    and existing_db_item.get('collected_at') is not None
                                    and not existing_db_item.get('upgrading_from')):  # not already an upgrade
                                try:
                                    _removal_item = {
                                        'type': existing_db_item['type'],
                                        'title': existing_db_item['title'],
                                        'imdb_id': existing_db_item.get('imdb_id'),
                                        'upgrading_from': existing_db_item.get('filled_by_file', ''),
                                        'filled_by_torrent_id': existing_db_item.get('filled_by_torrent_id'),
                                        'version': existing_db_item.get('version'),
                                        'season_number': existing_db_item.get('season_number'),
                                        'episode_number': existing_db_item.get('episode_number'),
                                        'filled_by_file': existing_db_item.get('filled_by_file'),
                                        'resolution': existing_db_item.get('resolution'),
                                        'location_on_disk': _old_location,
                                    }
                                    logging.info(f"[Collection] Replacement detected for {db_item_id} — removing old Plex entry at {_old_location!r}")
                                    remove_original_item_from_plex(_removal_item)
                                except Exception as _rep_cleanup_err:
                                    logging.debug(f"[Collection] Replacement Plex cleanup failed for {db_item_id}: {_rep_cleanup_err}")

                            # Secondary libraries share physical files — don't overwrite
                            # location_on_disk or ms_item_id that belong to the primary library.
                            if is_primary_library or not existing_db_item.get('location_on_disk'):
                                new_location = current_plex_location
                                new_ms_item_id = plex_ms_item_id
                            else:
                                new_location = existing_db_item.get('location_on_disk')
                                new_ms_item_id = existing_db_item.get('ms_item_id')

                            conn.execute('''
                                UPDATE media_items
                                SET state = ?, last_updated = ?, collected_at = ?,
                                    original_collected_at = COALESCE(original_collected_at, ?),
                                    location_on_disk = ?, upgraded = ?,
                                    resolution = COALESCE(?, resolution),
                                    size = COALESCE(?, size),
                                    ms_item_id = COALESCE(?, ms_item_id),
                                    scrape_results = NULL
                                WHERE id = ?
                            ''', (new_state, datetime.now(), collected_at, existing_collected_at,
                                  new_location, is_upgrade, item.get('resolution'), item.get('size_gb'),
                                  new_ms_item_id, db_item_id))

                            # If the Plex episode has a local:// guid (episode-level mismatch),
                            # schedule a GUID fix-match in background — show is matched but
                            # episode metadata is unresolved.
                            _ep_guid_str = str(item.get('guid') or '')
                            if _ep_guid_str.startswith('local://') and existing_db_item.get('type') == 'episode':
                                try:
                                    _fix_imdb  = existing_db_item.get('imdb_id')
                                    _fix_tmdb  = existing_db_item.get('tmdb_id')
                                    _fix_s     = existing_db_item.get('season_number')
                                    _fix_ep    = existing_db_item.get('episode_number')
                                    _fix_rk    = str(item.get('ratingKey') or '')
                                    _fix_show_rk = str(item.get('grandparentRatingKey') or _fix_rk)
                                    if (_fix_imdb or _fix_tmdb) and _fix_rk:
                                        # Check if show uses absolute numbering (anime) via battery
                                        _ci_is_absolute = False
                                        try:
                                            from cli_battery.app.database import Session as _BatSession, Item as _BatItem, Season as _BatSeason, Episode as _BatEp
                                            from sqlalchemy.orm import selectinload as _sil
                                            with _BatSession() as _bs:
                                                _bi = _bs.query(_BatItem).filter_by(imdb_id=_fix_imdb).first() if _fix_imdb else None
                                                if _bi:
                                                    _abs_check = _bs.query(_BatEp).join(_BatSeason).filter(
                                                        _BatSeason.item_id == _bi.id,
                                                        _BatEp.absolute_episode > 0
                                                    ).first()
                                                    _ci_is_absolute = _abs_check is not None
                                        except Exception:
                                            pass

                                        if not _ci_is_absolute:
                                            # Regular show — use show-level fix via force_match_with_tmdb
                                            logging.info(
                                                f"[PlexGUID] Episode {db_item_id} regular show local:// guid "
                                                f"— scheduling show-level fix-match (ratingKey={_fix_show_rk})"
                                            )
                                            import threading as _ci_thread
                                            def _ci_show_fix(_rk, _title, _year, _tmdb, _imdb, _s, _ep):
                                                try:
                                                    from utilities.plex_matching_functions import force_match_with_tmdb
                                                    force_match_with_tmdb(
                                                        _title,
                                                        str(_year) if _year else None,
                                                        str(_tmdb) if _tmdb else '0',
                                                        _rk,
                                                        imdb_id=_imdb,
                                                        media_type='show',
                                                        season=_s,
                                                        episode=_ep,
                                                    )
                                                except Exception as _fe:
                                                    logging.debug(f"[PlexGUID] Show fix-match failed: {_fe}")
                                            _ci_thread.Thread(
                                                target=_ci_show_fix,
                                                args=(_fix_show_rk, existing_db_item.get('title', ''),
                                                      existing_db_item.get('year'), _fix_tmdb, _fix_imdb,
                                                      _fix_s, _fix_ep),
                                                daemon=True
                                            ).start()
                                        else:
                                            logging.info(
                                                f"[PlexGUID] Episode {db_item_id} absolute show local:// guid "
                                                f"— scheduling episode fix-match (ratingKey={_fix_rk}, "
                                                f"S{_fix_s}E{_fix_ep})"
                                            )

                                        import threading as _ci_thread
                                        def _ci_fix(_rk, _title, _year, _tmdb, _imdb, _s, _ep):
                                            try:
                                                from cli_battery.app.direct_api import DirectAPI
                                                from utilities.settings import get_setting as _gs
                                                from plexapi.server import PlexServer as _PS
                                                _gu = _gs('Plex', 'url', '').rstrip('/')
                                                _gt = _gs('Plex', 'token', '')
                                                if not _gu or not _gt:
                                                    return
                                                _gplex = _PS(_gu, _gt, timeout=30)
                                                _gitem = _gplex.fetchItem(int(_rk))
                                                _gr = DirectAPI.get_plex_guid(_imdb, 'show', season=_s, episode=_ep) if _imdb else None
                                                _ep_guid  = (_gr or {}).get('episode_guid')
                                                _s_guid   = (_gr or {}).get('season_guid')
                                                _sh_guid  = (_gr or {}).get('show_guid')
                                                if _ep_guid:
                                                    _full_guid = f'plex://episode/{_ep_guid}'
                                                    _gname = f'{_title} S{_s:02d}E{_ep:02d}'
                                                elif _s_guid:
                                                    _full_guid = f'plex://season/{_s_guid}'
                                                    _gname = f'{_title} Season {_s}'
                                                elif _sh_guid:
                                                    _full_guid = f'plex://show/{_sh_guid}'
                                                    _gname = _title
                                                else:
                                                    logging.debug(f"[PlexGUID] No GUID for {_imdb} S{_s}E{_ep}")
                                                    return
                                                logging.info(f"[PlexGUID] Applying {_full_guid} to ratingKey={_rk}")
                                                import time as _t
                                                import requests as _req2
                                                from urllib.parse import quote as _uq2

                                                _match_url2 = (
                                                    f"{_gu}/library/metadata/{_rk}/match"
                                                    f"?guid={_uq2(_full_guid)}&name={_uq2(_gname)}"
                                                    f"&X-Plex-Token={_gt}"
                                                )
                                                _resp2 = _req2.put(_match_url2, timeout=15)
                                                _t.sleep(2)
                                                _req2.put(f"{_gu}/library/metadata/{_rk}/refresh?X-Plex-Token={_gt}", timeout=15)
                                                _t.sleep(4)
                                                _gitem.reload()
                                                _new_guid = str(getattr(_gitem, 'guid', ''))
                                                if not _new_guid.startswith('local://'):
                                                    logging.info(f"[PlexGUID] Episode fix SUCCESS '{_title}' S{_s}E{_ep} → {_new_guid}")
                                                else:
                                                    logging.warning(f"[PlexGUID] Episode fix failed (HTTP {_resp2.status_code}) '{_title}' S{_s}E{_ep} still local://")
                                            except Exception as _fe:
                                                logging.debug(f"[PlexGUID] Episode fix failed: {_fe}")
                                        if _ci_is_absolute:
                                            _ci_thread.Thread(
                                                target=_ci_fix,
                                                args=(_fix_rk, existing_db_item.get('title', ''),
                                                      existing_db_item.get('year'), _fix_tmdb, _fix_imdb,
                                                      _fix_s, _fix_ep),
                                                daemon=True
                                            ).start()
                                except Exception as _guid_err:
                                    logging.debug(f"[PlexGUID] Fix scheduling failed: {_guid_err}")

                            # Queue items for post-processing AFTER transaction commits
                            # This prevents database lock issues when post-processing tries to write to DB
                            # start_handle_state = time.time()
                            if new_state in ('Collected', 'Upgrading'):
                                item_for_processing = dict(conn.execute('SELECT * FROM media_items WHERE id = ?', (db_item_id,)).fetchone())
                                items_for_post_processing.append(item_for_processing)
                            # logging.debug(f"handle_state_change for item {db_item_id} took {time.time() - start_handle_state:.4f} seconds.")

                            cursor = conn.execute('SELECT * FROM media_items WHERE id = ?', (db_item_id,))
                            updated_item = cursor.fetchone()
                            cursor.close()
                            
                            if updated_item: # Ensure we got the updated item
                                updated_item_dict = dict(updated_item)
                                updated_item_dict['is_upgrade'] = is_upgrade # Pass the upgrade flag
                                if is_upgrade:
                                    notification_state = 'Upgraded' # Set state for notification if upgrade
                                else:
                                    notification_state = 'Collected' # Otherwise, it's collected
                                updated_item_dict['new_state'] = notification_state # Add the determined state
                                # Ensure original_collected_at is set correctly for the notification context
                                updated_item_dict['original_collected_at'] = updated_item_dict.get('original_collected_at') or existing_db_item.get('collected_at') or collected_at
                                # start_notification_time = time.time()
                                add_to_collected_notifications(updated_item_dict)
                                # logging.debug(f"add_to_collected_notifications for item {db_item_id} took {time.time() - start_notification_time:.4f} seconds.")

                                # Notify Bazarr via SignalR if integration is enabled.
                                # If probe_for_embedded_subtitles is on and all configured
                                # languages are already in the file, skip the notification
                                # so Bazarr doesn't redundantly search for them.
                                try:
                                    from routes.bazarr_signalr import notify_media_collected
                                    _skip_bazarr = False
                                    try:
                                        from utilities.settings import get_setting
                                        if get_setting('Subtitle Settings', 'probe_for_embedded_subtitles', False):
                                            _loc = updated_item_dict.get('location_on_disk')
                                            if _loc:
                                                from utilities.downsub import get_embedded_subtitle_languages, expand_languages
                                                from utilities.config.downsub_config import config as _sub_cfg
                                                _sub_cfg.reload()
                                                _wanted_langs = {
                                                    lang.alpha3
                                                    for lang in expand_languages(_sub_cfg.SUBTITLE_LANGUAGES)
                                                }
                                                if _wanted_langs:
                                                    import os as _os
                                                    _real = _os.path.realpath(_loc) if _os.path.islink(_loc) else _loc
                                                    _embedded = get_embedded_subtitle_languages(_real)
                                                    if _wanted_langs and _wanted_langs.issubset(_embedded):
                                                        logging.info(f"Bazarr notification skipped — all configured subtitle languages already embedded in {_loc}")
                                                        _skip_bazarr = True
                                    except Exception as _probe_err:
                                        logging.debug(f"Embedded subtitle probe for Bazarr check failed: {_probe_err}")
                                    if not _skip_bazarr:
                                        notify_media_collected(updated_item_dict, item.get('type', 'movie'))
                                except Exception as bazarr_err:
                                    logging.debug(f"Bazarr SignalR notification skipped: {bazarr_err}")
                            else:
                                logging.warning(f"Could not fetch updated item with ID {db_item_id} after update for notification.")
                        else:
                            # Item is already Collected/Upgrading
                            # Always update Plex-sourced fields (location, resolution, size) if they're missing or different
                            existing_location = existing_db_item.get('location_on_disk')
                            existing_size = existing_db_item.get('size')
                            existing_resolution = existing_db_item.get('resolution')
                            existing_imdb_id = existing_db_item.get('imdb_id')
                            existing_tmdb_id = existing_db_item.get('tmdb_id')
                            existing_collected_at = existing_db_item.get('collected_at')
                            new_size = item.get('size_gb')
                            new_resolution = item.get('resolution')

                            # Debug logging for backfill size issues
                            if backfill and (existing_size is None or existing_size == 0):
                                logging.info(f"[Backfill Debug] Item {db_item_id}: existing_size={existing_size}, new_size={new_size}, new_resolution={new_resolution}")

                            # For secondary libraries, don't let a different path trigger a location update —
                            # only allow location change if this is the primary library or location is unset.
                            effective_location = current_plex_location if (is_primary_library or not existing_location) else existing_location
                            location_changed = (existing_location != effective_location)

                            # Update if any Plex-sourced field is missing or location changed
                            should_update = (
                                location_changed or
                                ((existing_size is None or existing_size == 0) and new_size is not None and new_size > 0) or
                                (existing_resolution is None and new_resolution is not None) or
                                (not existing_imdb_id and imdb_id) or
                                (not existing_tmdb_id and tmdb_id) or
                                (existing_collected_at is None and collected_at is not None) or
                                (not existing_db_item.get('ms_item_id') and item.get('ratingKey'))
                            )

                            if should_update:
                                if backfill:
                                    if data_source == 'filesystem':
                                        logging.info(f"[Backfill] Updating file size for already-{existing_db_item['state']} item {db_item_id} ({item_identifier})")
                                    else:
                                        logging.info(f"[Backfill] Updating Plex fields (size/resolution) for already-{existing_db_item['state']} item {db_item_id} ({item_identifier})")
                                else:
                                    logging.debug(f"Updating missing metadata fields for already-{existing_db_item['state']} item {db_item_id} ({item_identifier})")
                                conn.execute('''
                                    UPDATE media_items
                                    SET location_on_disk = ?,
                                        resolution = COALESCE(?, resolution),
                                        size = COALESCE(?, size),
                                        imdb_id = COALESCE(NULLIF(imdb_id, ''), ?),
                                        tmdb_id = COALESCE(NULLIF(tmdb_id, ''), ?),
                                        collected_at = COALESCE(collected_at, ?),
                                        last_updated = ?,
                                        ms_item_id = COALESCE(ms_item_id, ?)
                                    WHERE id = ?
                                ''', (effective_location, new_resolution, new_size, imdb_id, tmdb_id, collected_at, datetime.now(), plex_ms_item_id, db_item_id))

                    else:
                        # --- NEW ITEM INSERT ---
                        # This item doesn't exist in DB (neither full path nor filename match)
                        logging.info(
                            f"Plex item {item_identifier} (location: {current_plex_location}, filename: {filename}) not found in existing_file_map. "
                            f"Proceeding to insert as new DB entry."
                        )

                        # Check if there are any items in 'Checking' state with matching identifiers
                        if checking_items_count > 0:
                            # Get the first checking item to use its version
                            checking_item_id = checking_item_ids_for_plex_item[0]
                            cursor = conn.execute('SELECT version FROM media_items WHERE id = ?', (checking_item_id,))
                            checking_item = cursor.fetchone()
                            cursor.close()
                            
                            if checking_item and checking_item['version']:
                                # Use the version from the checking item
                                version = checking_item['version']
                                # logging.info(f"Using version '{version}' from existing 'Checking' item (ID: {checking_item_id}) for {item_identifier}")
                            else:
                                # Fallback to parser if version not found
                                parsed_info = parser_approximation(filename)
                                version = parsed_info['version']
                                # logging.info(f"Checking item found but no version available, using parsed version '{version}' for {item_identifier}")
                        else:
                            # No checking items found, use parser
                            # start_insert_time = time.time()
                            parsed_info = parser_approximation(filename)
                            version = parsed_info['version']
                            # logging.debug(f"Using parsed version '{version}' for {item_identifier} (no checking items found)")

                        # GHOSTLIST CHECK: Skip inserting if this item is ghostlisted
                        ghostlist_check_query = '''
                            SELECT id FROM media_items
                            WHERE (imdb_id = ? OR tmdb_id = ?)
                            AND type = ?
                            AND (ghostlisted = 1 OR state = 'Blacklisted')
                            LIMIT 1
                        '''
                        ghostlist_check_params = (imdb_id, tmdb_id, item_type)
                        if item_type == 'episode':
                            ghostlist_check_query = '''
                                SELECT id FROM media_items
                                WHERE (imdb_id = ? OR tmdb_id = ?)
                                AND type = ?
                                AND season_number = ?
                                AND episode_number = ?
                                AND (ghostlisted = 1 OR state = 'Blacklisted')
                                LIMIT 1
                            '''
                            ghostlist_check_params = (imdb_id, tmdb_id, item_type, item['season_number'], item['episode_number'])

                        ghostlist_result = conn.execute(ghostlist_check_query, ghostlist_check_params).fetchone()
                        if ghostlist_result:
                            logging.info(f"⛔ Skipping {item_identifier} - user has ghostlisted/blacklisted this item (ID: {ghostlist_result['id']})")
                            continue

                        # WATCH HISTORY CHECK: Skip if item has been watched
                        if do_not_add_watched and watch_history_conn:
                            is_watched = False

                            if item_type == 'movie':
                                # Check movie watch history
                                if imdb_id or tmdb_id:
                                    # Tier 1: IMDb ID
                                    if imdb_id:
                                        query_wh = "SELECT 1 FROM watch_history WHERE type = 'movie' AND imdb_id = ?"
                                        if watch_history_conn.execute(query_wh, [imdb_id]).fetchone():
                                            is_watched = True

                                    # Tier 2: TMDb ID (fallback)
                                    if not is_watched and tmdb_id:
                                        query_wh = "SELECT 1 FROM watch_history WHERE type = 'movie' AND tmdb_id = ?"
                                        if watch_history_conn.execute(query_wh, [tmdb_id]).fetchone():
                                            is_watched = True

                            elif item_type == 'episode':
                                # Check episode watch history
                                season = item.get('season_number')
                                episode = item.get('episode_number')

                                if season is not None and episode is not None:
                                    # Tier 1: IMDb ID + season + episode (most reliable)
                                    if imdb_id:
                                        query_wh = """SELECT 1 FROM watch_history
                                                     WHERE type = 'episode' AND season = ? AND episode = ? AND imdb_id = ?"""
                                        if watch_history_conn.execute(query_wh, [season, episode, imdb_id]).fetchone():
                                            is_watched = True

                                    # Tier 2: TMDb ID + season + episode (fallback if no IMDb or not found)
                                    if not is_watched and tmdb_id:
                                        query_wh = """SELECT 1 FROM watch_history
                                                     WHERE type = 'episode' AND season = ? AND episode = ? AND tmdb_id = ?"""
                                        if watch_history_conn.execute(query_wh, [season, episode, tmdb_id]).fetchone():
                                            is_watched = True

                                    # Tier 3: show_title + season + episode (final fallback)
                                    if not is_watched and normalized_title:
                                        query_wh = """SELECT 1 FROM watch_history
                                                     WHERE type = 'episode' AND season = ? AND episode = ? AND show_title = ?"""
                                        if watch_history_conn.execute(query_wh, [season, episode, normalized_title]).fetchone():
                                            is_watched = True

                            if is_watched:
                                logging.info(f"⛔ Skipping {item_identifier} - item has been watched (watch history)")
                                continue

                        # Final dedup guard: block insert if same imdb+season+episode+version already
                        # exists in Collected, Adding, or Checking. Plex scans can race ahead of the
                        # queue — a file appears on the mount while the item is still in Adding/Checking,
                        # and without this guard a duplicate Collected row gets inserted.
                        _clean_ver = (version or '').rstrip('*')
                        logging.debug(f"[CollectedItems] dedup guard: imdb={imdb_id} tmdb={tmdb_id} type={item_type} s={item.get('season_number')} e={item.get('episode_number')} ver={_clean_ver!r}")
                        _dup_check = None
                        _id_col, _id_val = ('imdb_id', imdb_id) if imdb_id else ('tmdb_id', tmdb_id)
                        if _id_val:
                            if item_type == 'movie':
                                _dup_check = conn.execute(
                                    f"SELECT id, state FROM media_items WHERE {_id_col}=? AND type='movie' "
                                    "AND state IN ('Collected','Adding','Checking') "
                                    "AND REPLACE(version,'*','')=?",
                                    (_id_val, _clean_ver)
                                ).fetchone()
                            else:
                                _dup_check = conn.execute(
                                    f"SELECT id, state FROM media_items WHERE {_id_col}=? AND type='episode' "
                                    "AND season_number=? AND episode_number=? "
                                    "AND state IN ('Collected','Adding','Checking') "
                                    "AND REPLACE(version,'*','')=?",
                                    (_id_val, item.get('season_number'), item.get('episode_number'), _clean_ver)
                                ).fetchone()
                        if _dup_check:
                            logging.info(f"[CollectedItems] Skipping duplicate insert for {item_identifier} "
                                         f"(version={version}) — already exists as id={_dup_check[0]} state={_dup_check[1]}")
                            continue

                        if item_type == 'movie':
                            conn.execute('''
                                INSERT OR REPLACE INTO media_items
                                (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated, metadata_updated, version, collected_at, original_collected_at, genres, filled_by_file, runtime, location_on_disk, upgraded, country, resolution, physical_release_date, size, ms_item_id)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ''', (
                                imdb_id, tmdb_id, normalized_title, item.get('year'),
                                item.get('release_date'), 'Collected', 'movie',
                                datetime.now(), datetime.now(), version, collected_at, collected_at, genres, filename, item.get('runtime'), current_plex_location, False, item.get('country', '').lower(), item.get('resolution'), item.get('physical_release_date'), item.get('size_gb'), plex_ms_item_id
                            ))
                        else:
                            if imdb_id not in airtime_cache:
                                airtime_cache[imdb_id] = get_existing_airtime(conn, imdb_id)
                                if airtime_cache[imdb_id] is None:
                                    airtime_cache[imdb_id] = get_show_airtime_by_imdb_id(imdb_id)
                                if not airtime_cache[imdb_id]:
                                    airtime_cache[imdb_id] = '19:00'
                            
                            airtime = airtime_cache[imdb_id]

                            # Inherit NZB fields from a Collected sibling of the same season pack.
                            # Prevents new Plex-inserted episodes from re-entering the pipeline
                            # and submitting duplicate NZB jobs to cli_mount.
                            _sibling_torrent_id = None
                            _sibling_magnet = None
                            _sibling_orig_title = None
                            _sibling_dfn = None
                            try:
                                _sib = conn.execute(
                                    "SELECT filled_by_torrent_id, filled_by_magnet, original_scraped_torrent_title, debrid_folder_name "
                                    "FROM media_items "
                                    "WHERE imdb_id=? AND season_number=? AND type='episode' AND state='Collected' "
                                    "AND filled_by_torrent_id IS NOT NULL LIMIT 1",
                                    (imdb_id, item['season_number'])
                                ).fetchone()
                                if _sib:
                                    _sibling_torrent_id = _sib['filled_by_torrent_id']
                                    _sibling_magnet = _sib['filled_by_magnet']
                                    _sibling_orig_title = _sib['original_scraped_torrent_title']
                                    _sibling_dfn = _sib['debrid_folder_name']
                                    logging.debug(f"[CollectedItems] Inheriting NZB fields from sibling for {item_identifier}")
                            except Exception:
                                pass

                            cursor = conn.execute('''
                                INSERT OR REPLACE INTO media_items
                                (imdb_id, tmdb_id, title, year, release_date, state, type, season_number, episode_number, episode_title, last_updated, metadata_updated, version, airtime, collected_at, original_collected_at, genres, filled_by_file, runtime, location_on_disk, upgraded, country, resolution, size, ms_item_id, filled_by_torrent_id, filled_by_magnet, original_scraped_torrent_title, debrid_folder_name)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ''', (
                                imdb_id, tmdb_id, normalized_title, item.get('year'),
                                item.get('release_date'), 'Collected', 'episode',
                                item['season_number'], item['episode_number'], item.get('episode_title', ''),
                                datetime.now(), datetime.now(), version, airtime, collected_at, collected_at, genres, filename, item.get('runtime'), current_plex_location, False, item.get('country', '').lower(), item.get('resolution'), item.get('size_gb'), plex_ms_item_id,
                                _sibling_torrent_id, _sibling_magnet, _sibling_orig_title, _sibling_dfn
                            ))
                        # logging.debug(f"Inserting new item {item_identifier} (from Plex file: {filename}, location: {current_plex_location}) took {time.time() - start_insert_time:.4f} seconds.")
                        logging.info(f"Added new item {item_identifier} (file: {filename}) to collection.")

                        # Queue newly-inserted items for post-processing (e.g. custom script, overlays)
                        # The UPDATE path does this at line ~541; INSERT path was previously missing it.
                        new_item_id = cursor.lastrowid
                        if new_item_id:
                            new_item_row = conn.execute('SELECT * FROM media_items WHERE id = ?', (new_item_id,)).fetchone()
                            if new_item_row:
                                items_for_post_processing.append(dict(new_item_row))


            except Exception as e:
                logging.error(f"Error processing item {item_identifier}: {str(e)}", exc_info=True)
                continue
            # finally:
                # logging.debug(f"Finished processing item {item_identifier} in {time.time() - start_time_item:.4f} seconds.")

        # logging.info(f"Finished processing main batch loop in {time.time() - start_time_batch:.4f} seconds.")

        # --- Post-loop cleanup ---
        # Skip cleanup during backfill - backfill only updates existing records with source metadata (filesystem or Plex)
        # and should not delete items that might not have been scanned yet
        if not recent and not backfill:
            # logging.info("Starting post-loop cleanup for missing files.")
            # start_cleanup_time = time.time()
            cursor = conn.execute('''
                SELECT id, imdb_id, tmdb_id, title, type, season_number, episode_number, state, version,
                       filled_by_file, collected_at, release_date, upgrading_from, location_basename, location_on_disk
                FROM media_items
                WHERE state = 'Collected'
            ''')
            for row in cursor:
                item = row_to_dict(row)
                item_identifier = generate_identifier(item)

                filled_by = item.get('filled_by_file')
                location_base = item.get('location_basename')
                location_on_disk = item.get('location_on_disk')

                # A file is considered present if any of these are in the scan results:
                # - filled_by_file (filename)
                # - location_basename (filename)
                # - location_on_disk (full path - important for symlink mode with multiple versions)
                is_present_on_disk = (filled_by and filled_by in all_valid_filenames) or \
                                     (location_base and location_base in all_valid_filenames) or \
                                     (location_on_disk and location_on_disk in all_valid_filenames)

                # A file is considered expected if it has at least one filename/path associated with it
                is_expected_on_disk = filled_by or location_base or location_on_disk

                # Debug: Log cleanup check for items that might be deleted
                if is_expected_on_disk and not is_present_on_disk:
                    logging.debug(f"[Cleanup Debug] Item {item['id']} ({item_identifier}) NOT found in Plex scan:")
                    logging.debug(f"  filled_by_file: '{filled_by}' - in valid? {filled_by in all_valid_filenames if filled_by else 'N/A'}")
                    logging.debug(f"  location_basename: '{location_base}' - in valid? {location_base in all_valid_filenames if location_base else 'N/A'}")
                    logging.debug(f"  location_on_disk: '{location_on_disk}' - in valid? {location_on_disk in all_valid_filenames if location_on_disk else 'N/A'}")
                    logging.debug(f"  all_valid_filenames sample (first 5): {list(all_valid_filenames)[:5]}")

                if is_expected_on_disk and not is_present_on_disk:
                    file_to_log = location_base or filled_by
                    # This item's file is considered missing
                    if get_setting("Debug", "rescrape_missing_files", default=False):
                        try:
                            # Check if another version of this item already exists in 'Collected' state
                            current_version = item['version'].strip('*') if item.get('version') else ''
                            
                            # Build query based on item type to find other collected versions
                            if item['type'] == 'movie':
                                matching_cursor = conn.execute('''
                                    SELECT id, version FROM media_items 
                                    WHERE (imdb_id = ? OR (tmdb_id IS NOT NULL AND tmdb_id = ?)) AND type = 'movie' AND state = 'Collected' AND id != ?
                                ''', (item['imdb_id'], item['tmdb_id'], item['id']))
                            else: # episode
                                matching_cursor = conn.execute('''
                                    SELECT id, version FROM media_items 
                                    WHERE (imdb_id = ? OR (tmdb_id IS NOT NULL AND tmdb_id = ?)) AND type = 'episode' AND season_number = ? AND episode_number = ? AND state = 'Collected' AND id != ?
                                ''', (item['imdb_id'], item['tmdb_id'], item['season_number'], item['episode_number'], item['id']))
                            
                            matching_items = matching_cursor.fetchall()
                            matching_cursor.close()
                            
                            matching_version_exists = any(
                                (m['version'].strip('*') if m.get('version') else '') == current_version 
                                for m in matching_items
                            )
                            
                            if matching_version_exists:
                                logging.info(f"[Missing File Cleanup] Deleting item {item_identifier} (ID: {item['id']}, File: {file_to_log}) as another collected version ('{current_version}') exists.")
                                conn.execute('DELETE FROM media_items WHERE id = ?', (item['id'],))
                            else:
                                # GHOSTLIST CHECK: Don't move ghostlisted/blacklisted items to Wanted
                                is_ghostlisted = item.get('ghostlisted') == 1
                                is_blacklisted = item.get('state') == 'Blacklisted'

                                if is_ghostlisted or is_blacklisted:
                                    logging.info(f"⛔ [Missing File Cleanup] File missing for {'ghostlisted' if is_ghostlisted else 'blacklisted'} item {item_identifier} (ID: {item['id']}, File: {file_to_log}). Deleting item instead of moving to Wanted.")
                                    conn.execute('DELETE FROM media_items WHERE id = ?', (item['id'],))
                                else:
                                    logging.info(f"[Missing File Cleanup] File missing for {item_identifier} (ID: {item['id']}, File: {file_to_log}). No other matching version found. Moving to 'Wanted'.")
                                    conn.execute(f'''
                                        UPDATE media_items
                                        SET state = 'Wanted',
                                            filled_by_file = NULL,
                                            filled_by_title = NULL,
                                            filled_by_magnet = NULL,
                                            filled_by_torrent_id = NULL,
                                            {RESET_COLLECTION_STATE_SQL},
                                            last_updated = ?,
                                            version = TRIM(version, '*')
                                        WHERE id = ?
                                    ''', (datetime.now(), item['id']))
                        except Exception as e:
                            # conn.rollback() # Rollback for THIS item was removed, transaction handles overall
                            logging.error(f"Error handling missing file for item {item_identifier} (ID: {item['id']}): {str(e)}", exc_info=True)
                    else: # rescrape_missing_files is False
                        logging.info(f"[Missing File Cleanup] File missing for {item_identifier} (ID: {item['id']}, File: {file_to_log}). 'rescrape_missing_files' is False. Deleting item.")
                        conn.execute('''
                            DELETE FROM media_items
                            WHERE id = ?
                        ''', (item['id'],))
            cursor.close()
            # logging.info(f"Finished post-loop cleanup in {time.time() - start_cleanup_time:.4f} seconds.")

        conn.commit()

        # Process post-processing tasks AFTER transaction commits
        # This prevents database lock issues from Plex label application and other operations
        if items_for_post_processing:
            logging.info(f"Running post-processing for {len(items_for_post_processing)} items")
            for item in items_for_post_processing:
                try:
                    handle_state_change(item)
                except Exception as e:
                    logging.error(f"Error in post-processing for item {item.get('id')}: {str(e)}", exc_info=True)

    except Exception as e:
        logging.error(f"Error adding collected items: {str(e)}", exc_info=True)
        conn.rollback()
        raise
    finally:
        conn.close()
        if watch_history_conn:
            watch_history_conn.close()

def plex_collection_disabled(media_items_batch: List[Dict[str, Any]]) -> bool:
    """
    Simplified collection process when Plex library checks are disabled.
    This function handles the basic database operations needed for collecting items
    without Plex library integration.

    Process:
    1. Get versions from config
    2. Check if version is in filename (from location or filled_by_file)
    3. If not, use parser_approximation
    4. Check for existing items with same version
    5. Add to database if new

    Args:
        media_items_batch (List[Dict[str, Any]]): List of media items to process

    Returns:
        bool: True if all operations were successful, False otherwise
    """
    if not media_items_batch:
        return True

    from utilities.settings import load_config
    from utilities.reverse_parser import parser_approximation
    
    # Get versions from config
    config = load_config()
    version_list = list(config.get('Scraping', {}).get('versions', {}).keys())
    if not version_list:
        logging.warning("No versions configured in Scraping config, using empty list")
        version_list = []

    conn = get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        
        for item in media_items_batch:
            # Get filename from either location or filled_by_file
            filename = None
            filename_source = None
            locations = item.get('location', [])
            if isinstance(locations, str):
                locations = [locations]
            
            # Try to get filename from locations first
            for location in locations:
                if location:
                    filename = os.path.basename(location)
                    if filename:
                        filename_source = 'location'
                        break
            
            # If no filename found in locations, try filled_by_file
            if not filename and item.get('filled_by_file'):
                filename = item['filled_by_file']
                filename_source = 'filled_by_file'

            # If we still don't have a filename, use the title as fallback
            if not filename and item.get('title'):
                filename = item['title']
                if item.get('year'):
                    filename += f" ({item['year']})"
                filename_source = 'title'

            if not filename:
                logging.warning(f"Could not determine filename for item (Title: {item.get('title', 'Unknown')}, Type: {item.get('type', 'Unknown')})")
                continue

            logging.debug(f"Using filename from {filename_source}: {filename}")
            found_version = None

            # Check if any version from the list is in the filename
            for version in version_list:
                if version.lower() in filename.lower():
                    found_version = version
                    logging.debug(f"Found version {version} in filename")
                    break

            # If no version found, try parser_approximation
            if not found_version:
                parsed_result = parser_approximation(filename)
                found_version = parsed_result.get('version')
                if found_version:
                    logging.debug(f"Found version {found_version} using parser_approximation")

            if not found_version:
                logging.warning(f"Could not determine version for {filename_source}: {filename}")
                continue

            # Check if item with this version already exists
            cursor = conn.cursor()
            query_params = []
            
            # Build query conditions based on available IDs
            id_conditions = []
            if item.get('imdb_id'):
                id_conditions.append('imdb_id = ?')
                query_params.append(item['imdb_id'])
            if item.get('tmdb_id'):
                id_conditions.append('tmdb_id = ?')
                query_params.append(item['tmdb_id'])
            
            if not id_conditions:
                logging.warning(f"No IMDb or TMDb ID available for {filename}")
                continue
                
            id_query = ' OR '.join(id_conditions)
            query_params.append(found_version)
            
            if item.get('type') == 'episode':
                query = f'''
                    SELECT id FROM media_items
                    WHERE ({id_query})
                    AND REPLACE(version, '*', '') = REPLACE(?, '*', '')
                    AND state = 'Collected'
                    AND type = 'episode'
                    AND season_number = ?
                    AND episode_number = ?
                '''
                query_params.extend([item.get('season_number'), item.get('episode_number')])
            else:
                # Movie case
                query = f'''
                    SELECT id FROM media_items
                    WHERE ({id_query})
                    AND REPLACE(version, '*', '') = REPLACE(?, '*', '')
                    AND state = 'Collected'
                    AND type = 'movie'
                '''
            
            cursor.execute(query, query_params)
            existing_item = cursor.fetchone()
            if existing_item:
                item_desc = f"S{item.get('season_number')}E{item.get('episode_number')}" if item.get('type') == 'episode' else "movie"
                logging.info(f"Item already exists ({item_desc}) with version {found_version} (from {filename_source}): {filename}")
                continue

            # GHOSTLIST CHECK: Skip inserting if this item is ghostlisted or blacklisted
            ghostlist_check_query = '''
                SELECT id FROM media_items
                WHERE (imdb_id = ? OR tmdb_id = ?)
                AND type = ?
                AND (ghostlisted = 1 OR state = 'Blacklisted')
                LIMIT 1
            '''
            ghostlist_check_params = [item.get('imdb_id'), item.get('tmdb_id'), item.get('type', 'movie')]

            if item.get('type') == 'episode':
                ghostlist_check_query = '''
                    SELECT id FROM media_items
                    WHERE (imdb_id = ? OR tmdb_id = ?)
                    AND type = ?
                    AND season_number = ?
                    AND episode_number = ?
                    AND (ghostlisted = 1 OR state = 'Blacklisted')
                    LIMIT 1
                '''
                ghostlist_check_params = [
                    item.get('imdb_id'),
                    item.get('tmdb_id'),
                    item.get('type'),
                    item.get('season_number'),
                    item.get('episode_number')
                ]

            ghostlist_result = cursor.execute(ghostlist_check_query, ghostlist_check_params).fetchone()
            if ghostlist_result:
                item_desc = f"S{item.get('season_number')}E{item.get('episode_number')}" if item.get('type') == 'episode' else "movie"
                logging.info(f"⛔ Skipping {item_desc} {item.get('title')} - user has ghostlisted/blacklisted this item (ID: {ghostlist_result['id']})")
                continue

            # Add new item to database
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            collected_at = item.get('collected_at', now)  # Use item's collected_at if available, fallback to now

            if item.get('type') == 'episode':
                cursor.execute('''
                    INSERT INTO media_items 
                    (imdb_id, tmdb_id, title, type, season_number, episode_number, version, collected_at, state, filled_by_file,
                     year, release_date, last_updated, metadata_updated)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Collected', ?, ?, ?, ?, ?)
                ''', (
                    item.get('imdb_id'),
                    item.get('tmdb_id'),
                    item.get('title'),
                    item.get('type'),
                    item.get('season_number'),
                    item.get('episode_number'),
                    found_version,
                    collected_at,  # Use collected_at from item
                    filename,
                    item.get('year'),
                    item.get('release_date'),
                    now,
                    now
                ))
            else:
                cursor.execute('''
                    INSERT INTO media_items 
                    (imdb_id, tmdb_id, title, type, version, collected_at, state, filled_by_file,
                     year, release_date, last_updated, metadata_updated)
                    VALUES (?, ?, ?, ?, ?, ?, 'Collected', ?, ?, ?, ?, ?)
                ''', (
                    item.get('imdb_id'),
                    item.get('tmdb_id'),
                    item.get('title'),
                    item.get('type'),
                    found_version,
                    collected_at,  # Use collected_at from item
                    filename,
                    item.get('year'),
                    item.get('release_date'),
                    now,
                    now
                ))
            
            logging.info(f"Added new item to collection with version {found_version} (from {filename_source}): {filename}")

        conn.commit()
        return True

    except Exception as e:
        logging.error(f"Error in plex_collection_disabled: {str(e)}")
        if conn:
            conn.rollback()
        return False

    finally:
        if conn:
            conn.close()

def generate_identifier(item: Dict[str, Any]) -> str:
    if item.get('type') == 'movie':
        return f"{item.get('title')} ({item.get('year')})"
    else:
        season = item.get('season_number', '00')
        episode = item.get('episode_number', '00')
        
        # Convert to int if possible, otherwise use string formatting
        try:
            season = f"{int(season):02d}"
        except (ValueError, TypeError):
            season = str(season).zfill(2)
        
        try:
            episode = f"{int(episode):02d}"
        except (ValueError, TypeError):
            episode = str(episode).zfill(2)
        
        return f"{item.get('title')} S{season}E{episode}"

def remove_original_item_from_plex(item: Dict[str, Any]) -> bool:
    """Remove the old file from the media server and disk.

    Uses DeletionManager.remove_from_media_server so Plex, Jellyfin, and the
    scan+trash fallback are all handled automatically.

    For symlink mode the symlink is also deleted directly from disk first so
    that Plex's 'Allow media deletion' permission is not a hard requirement.

    Returns True if the media-server removal succeeded.
    """
    item_identifier = f"{item['type']}_{item['title']}_{item['imdb_id']}"
    original_file_path = item.get('upgrading_from')

    if not original_file_path or not item.get('title'):
        logging.warning(f"[Cleanup] No file path or title for: {item_identifier}")
        return False

    # Construct a minimal item dict targeting the OLD file so DeletionManager
    # removes the right entry — not the new (current) version.
    old_item = {
        'title':          item['title'],
        'type':           item['type'],
        'imdb_id':        item.get('imdb_id'),
        'season_number':  item.get('season_number'),
        'episode_number': item.get('episode_number'),
        # DeletionManager checks location_on_disk first, then filled_by_file
        'location_on_disk': item.get('location_on_disk'),
        'filled_by_file':   original_file_path,
    }

    try:
        from utilities.deletion_manager import DeletionManager
        dm = DeletionManager()

        # Symlink mode: delete the symlink from disk directly first —
        # independent of Plex's 'Allow media deletion' permission.
        if dm.using_symlinks:
            symlink_path = item.get('location_on_disk') or ''
            if symlink_path and os.path.islink(symlink_path):
                try:
                    os.unlink(symlink_path)
                    logging.info(f"[Cleanup] Deleted old symlink: {symlink_path}")
                except Exception as e:
                    logging.error(f"[Cleanup] Failed to delete symlink {symlink_path}: {e}")
            elif symlink_path:
                logging.warning(f"[Cleanup] Old symlink not found on disk: {symlink_path}")

        # Remove from media server (Plex / Jellyfin) — has scan+trash fallback built in
        result = dm.remove_from_media_server(old_item)
        if not result.get('success'):
            logging.error(f"[Cleanup] Media server removal failed for {item_identifier}: {result.get('error')}")
        return result.get('success', False)

    except Exception as e:
        logging.error(f"[Cleanup] remove_original_item_from_plex failed for {item_identifier}: {e}", exc_info=True)
        return False


def remove_original_item_from_account(item: Dict[str, Any]):
    """Remove the old torrent from the debrid account."""
    from queues.adding_queue import AddingQueue
    original_torrent_id = item.get('filled_by_torrent_id')

    if original_torrent_id:
        adding_queue = AddingQueue()
        adding_queue.remove_unwanted_torrent(original_torrent_id)
    else:
        logging.warning(f"[Cleanup] No torrent ID to remove for: {item.get('title', 'unknown')}")

def remove_original_item_from_results(item: Dict[str, Any], media_items_batch: List[Dict[str, Any]]):
    try:
        original_file_path = item.get('upgrading_from')
        if original_file_path:
            original_filename = os.path.basename(original_file_path)
            media_items_batch[:] = [batch_item for batch_item in media_items_batch 
                                    if not any(os.path.basename(loc) == original_filename 
                                               for loc in batch_item.get('location', [])
                                               if isinstance(loc, str))]
        else:
            logging.warning(f"No original file path found for {generate_identifier(item)}")
    except Exception as e:
        logging.error(f"Error in remove_original_item_from_results: {str(e)}", exc_info=True)

# --- START: New function to add/update TV show ---
def add_or_update_tv_show(imdb_id: str, tmdb_id: Optional[str] = None, title: Optional[str] = None, year: Optional[int] = None, status: Optional[str] = None):
    """
    Adds a new TV show to the tv_shows table or updates an existing one.
    This is typically called when a show is first encountered during metadata processing.
    Completeness checks are handled by a separate periodic task.

    Args:
        imdb_id (str): The IMDb ID of the show (required).
        tmdb_id (str, optional): The TMDB ID of the show.
        title (str, optional): The title of the show.
        year (int, optional): The release year of the show.
        status (str, optional): The current status of the show (e.g., 'Ended', 'Continuing').
    """
    if not imdb_id:
        logging.error("[TV Show Upsert] Attempted to add/update TV show without an IMDb ID.")
        return

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        now_utc = datetime.now(timezone.utc)
        # Convert to string format compatible with SQLite DATETIME
        now_str = now_utc.strftime('%Y-%m-%d %H:%M:%S')


        # Data for the INSERT part
        insert_data = (
            imdb_id,
            tmdb_id,
            title,
            year,
            status,
            False, # Default is_complete to False initially
            None,  # Default total_episodes to None initially
            None,  # last_status_check is initially None (set by periodic task)
            now_str,   # Set added_at timestamp
            now_str    # Set last_updated timestamp
        )

        # Use INSERT ... ON CONFLICT for atomic upsert based on imdb_id
        # We only update fields that might change or need refreshing.
        # We specifically DO NOT update is_complete or total_episodes here.
        # last_status_check is also not updated here.
        # Using COALESCE prevents overwriting existing values with NULL if new metadata is missing fields.
        # Use NULLIF to treat empty strings as NULL for proper COALESCE behavior.
        cursor.execute("""
            INSERT INTO tv_shows (
                imdb_id, tmdb_id, title, year, status, is_complete,
                total_episodes, last_status_check, added_at, last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(imdb_id) DO UPDATE SET
                tmdb_id = COALESCE(NULLIF(excluded.tmdb_id, ''), tmdb_id),
                title = COALESCE(NULLIF(excluded.title, ''), title),
                year = COALESCE(excluded.year, year),
                status = COALESCE(NULLIF(excluded.status, ''), status),
                last_updated = excluded.last_updated
            WHERE imdb_id = excluded.imdb_id;
        """, insert_data)

        conn.commit()
        if cursor.rowcount > 0:
            logging.debug(f"[TV Show Upsert] Successfully added or updated show: IMDb ID {imdb_id}")
        else:
            # This might happen if the ON CONFLICT update resulted in no actual change
            logging.debug(f"[TV Show Upsert] No rows affected for show IMDb ID {imdb_id} (likely no change needed).")

    except sqlite3.Error as db_err:
        logging.error(f"[TV Show Upsert] Database error for show IMDb ID {imdb_id}: {db_err}", exc_info=True)
        if conn:
            conn.rollback()
    except Exception as err:
        logging.error(f"[TV Show Upsert] Unexpected error for show IMDb ID {imdb_id}: {err}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()
# --- END: New function ---
