from .core import get_db_connection, row_to_dict, normalize_string, get_existing_airtime
import logging
import os
from datetime import datetime, timezone, timedelta
import json
from .database_writing import add_to_collected_notifications, update_media_item_state
from utilities.reverse_parser import parser_approximation
from utilities.settings import get_setting
from typing import Dict, Any, List, Optional
from utilities.post_processing import handle_state_change
from cli_battery.app.direct_api import DirectAPI
import sqlite3
import time

def add_collected_items(media_items_batch, recent=False):
    from datetime import datetime, timedelta
    from utilities.settings import get_setting
    from queues.upgrading_queue import log_successful_upgrade
    from metadata.metadata import get_show_airtime_by_imdb_id

    # Check if Plex library checks are disabled
    if get_setting('Plex', 'disable_plex_library_checks', default=False):
        logging.info("Plex library checks disabled - using simplified collection process")
        return plex_collection_disabled(media_items_batch)

    conn = get_db_connection()
    try:
        conn.execute('BEGIN TRANSACTION')
        
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
            batch_size = 450  # Will become 900 when doubled (under 999 limit)
            filenames_list = list(filenames_in_batch)
            existing_items = []
            
            for i in range(0, len(filenames_list), batch_size):
                batch = filenames_list[i:i + batch_size]
                placeholders = ', '.join(['?'] * len(batch))
                query = f'''
                    SELECT id, imdb_id, tmdb_id, title, type, season_number, episode_number, state, version,
                           filled_by_file, collected_at, release_date, upgrading_from, content_source
                    FROM media_items
                    WHERE filled_by_file IN ({placeholders})
                       OR upgrading_from IN ({placeholders})
                '''
                params = batch * 2  # Parameters for both placeholders
                cursor = conn.execute(query, params)
                existing_items.extend(cursor.fetchall())
                cursor.close()
            
            # Process the results
            for row in existing_items:
                filled_by_file = row['filled_by_file']
                upgrading_from = os.path.basename(row['upgrading_from'] or '')
                state = row['state']

                if state == 'Collected':
                    existing_collected_files.add(filled_by_file)
                if state == 'Upgrading':
                    if filled_by_file:
                        existing_collected_files.add(filled_by_file)
                    if upgrading_from:
                        upgrading_from_files.add(upgrading_from)

                if filled_by_file:
                    existing_file_map[filled_by_file] = row_to_dict(row)
                if upgrading_from:
                    existing_file_map[upgrading_from] = row_to_dict(row)

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
                    if filename and filename not in existing_collected_files and filename not in upgrading_from_files:
                        new_locations.append(location)
                    else:
                        filtered_out_files.add(filename)
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

        # logging.info(f"Starting processing of {len(filtered_media_items_batch)} filtered media items.")
        # start_time_batch = time.time()

        for index, item in enumerate(filtered_media_items_batch):
            item_identifier = generate_identifier(item)
            # start_time_item = time.time()
            
            plex_locations = item.get('location', [])
            if isinstance(plex_locations, str):
                plex_locations = [plex_locations]

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
                    if filename and filename not in filtered_out_files:
                        all_valid_filenames.add(filename)
                        
                imdb_id = item.get('imdb_id') or None
                tmdb_id = item.get('tmdb_id') or None
                normalized_title = normalize_string(item.get('title', 'Unknown'))
                item_type = 'episode' if 'season_number' in item and 'episode_number' in item else 'movie'

                if imdb_id is None and tmdb_id is None:
                    logging.warning(f"Skipping unmatched Plex item: {item.get('title', 'Unknown')} from location(s): {plex_locations}")
                    continue

                # Iterate through each file path provided by Plex for this media item
                for current_plex_location in plex_locations:
                    filename = os.path.basename(current_plex_location) # This is the filename from Plex

                    added_at = item.get('addedAt')
                    if added_at is not None:
                        collected_at = datetime.fromtimestamp(added_at)
                    else:
                        collected_at = datetime.now()
                    genres = json.dumps(item.get('genres', []))

                    if filename in existing_file_map:
                        existing_db_item = existing_file_map[filename]
                        db_item_id = existing_db_item['id']
                        
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

                            if is_upgrade and get_setting("Scraping", "enable_upgrading_cleanup", default=False):
                                upgrade_item = {
                                    'type': existing_db_item['type'],
                                    'title': existing_db_item['title'],
                                    'imdb_id': existing_db_item['imdb_id'],
                                    'upgrading_from': existing_db_item['upgrading_from'],
                                    'filled_by_torrent_id': existing_db_item.get('filled_by_torrent_id'),
                                    'version': existing_db_item['version'],
                                    'season_number': existing_db_item.get('season_number'),
                                    'episode_number': existing_db_item.get('episode_number'),
                                    'filled_by_file': existing_db_item.get('filled_by_file'),
                                    'resolution': existing_db_item.get('resolution')  # Preserve old resolution for reference
                                }
                                
                                if upgrade_item['filled_by_file'] != upgrade_item['upgrading_from']:
                                    conn.execute('''
                                        UPDATE media_items
                                        SET upgraded = 1
                                        WHERE id = ?
                                    ''', (db_item_id,))
                                    
                                    remove_original_item_from_plex(upgrade_item)
                                    remove_original_item_from_account(upgrade_item)
                                    remove_original_item_from_results(upgrade_item, media_items_batch)
                                    log_successful_upgrade(upgrade_item)
                                
                            existing_collected_at = existing_db_item.get('collected_at') or collected_at

                            conn.execute('''
                                UPDATE media_items
                                SET state = ?, last_updated = ?, collected_at = ?, 
                                    original_collected_at = COALESCE(original_collected_at, ?),
                                    location_on_disk = ?, upgraded = ?, resolution = ?
                                WHERE id = ?
                            ''', (new_state, datetime.now(), collected_at, existing_collected_at, 
                                  current_plex_location, is_upgrade, item.get('resolution'), db_item_id))

                            # Add post-processing call after state update
                            # start_handle_state = time.time()
                            if new_state == 'Collected':
                                handle_state_change(dict(conn.execute('SELECT * FROM media_items WHERE id = ?', (db_item_id,)).fetchone()))
                            elif new_state == 'Upgrading':
                                handle_state_change(dict(conn.execute('SELECT * FROM media_items WHERE id = ?', (db_item_id,)).fetchone()))
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
                            else:
                                logging.warning(f"Could not fetch updated item with ID {db_item_id} after update for notification.")
                        # else:
                             # logging.debug(
                             #     f"DB Item ID {db_item_id} ({item_identifier}, file: {existing_db_item['filled_by_file']}) "
                             #     f"is already '{existing_db_item['state']}'. Skipping state update and notification. "
                             #     f"Plex item location: {current_plex_location}."
                             # )

                    else:
                        # --- NEW ITEM INSERT ---
                        logging.info(
                            f"Plex item {item_identifier} (location: {current_plex_location}, filename: {filename}) not found in existing_file_map. "
                            f"Proceeding to insert as new DB entry. "
                            # f"Found {checking_items_count} existing 'Checking' item(s) for these identifiers (IDs: {checking_item_ids_for_plex_item})."
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

                        if item_type == 'movie':
                            conn.execute('''
                                INSERT OR REPLACE INTO media_items
                                (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated, metadata_updated, version, collected_at, original_collected_at, genres, filled_by_file, runtime, location_on_disk, upgraded, country, resolution, physical_release_date)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ''', (
                                imdb_id, tmdb_id, normalized_title, item.get('year'),
                                item.get('release_date'), 'Collected', 'movie',
                                datetime.now(), datetime.now(), version, collected_at, collected_at, genres, filename, item.get('runtime'), current_plex_location, False, item.get('country', '').lower(), item.get('resolution'), item.get('physical_release_date')
                            ))
                        else:
                            if imdb_id not in airtime_cache:
                                airtime_cache[imdb_id] = get_existing_airtime(conn, imdb_id)
                                if airtime_cache[imdb_id] is None:
                                    airtime_cache[imdb_id] = get_show_airtime_by_imdb_id(imdb_id)
                                if not airtime_cache[imdb_id]:
                                    airtime_cache[imdb_id] = '19:00'
                            
                            airtime = airtime_cache[imdb_id]
                            conn.execute('''
                                INSERT OR REPLACE INTO media_items
                                (imdb_id, tmdb_id, title, year, release_date, state, type, season_number, episode_number, episode_title, last_updated, metadata_updated, version, airtime, collected_at, original_collected_at, genres, filled_by_file, runtime, location_on_disk, upgraded, country, resolution)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ''', (
                                imdb_id, tmdb_id, normalized_title, item.get('year'),
                                item.get('release_date'), 'Collected', 'episode',
                                item['season_number'], item['episode_number'], item.get('episode_title', ''),
                                datetime.now(), datetime.now(), version, airtime, collected_at, collected_at, genres, filename, item.get('runtime'), current_plex_location, False, item.get('country', '').lower(), item.get('resolution')
                            ))
                        # logging.debug(f"Inserting new item {item_identifier} (from Plex file: {filename}, location: {current_plex_location}) took {time.time() - start_insert_time:.4f} seconds.")
                        logging.info(f"Added new item {item_identifier} (file: {filename}) to collection.")


            except Exception as e:
                logging.error(f"Error processing item {item_identifier}: {str(e)}", exc_info=True)
                continue
            # finally:
                # logging.debug(f"Finished processing item {item_identifier} in {time.time() - start_time_item:.4f} seconds.")

        # logging.info(f"Finished processing main batch loop in {time.time() - start_time_batch:.4f} seconds.")

        # --- Post-loop cleanup ---
        if not recent:
            # logging.info("Starting post-loop cleanup for missing files.")
            # start_cleanup_time = time.time()
            cursor = conn.execute('''
                SELECT id, imdb_id, tmdb_id, title, type, season_number, episode_number, state, version, filled_by_file, collected_at, release_date, upgrading_from
                FROM media_items
                WHERE state = 'Collected'
            ''')
            for row in cursor:
                item = row_to_dict(row)
                item_identifier = generate_identifier(item)
                if item['filled_by_file'] and item['filled_by_file'] not in all_valid_filenames:
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
                                logging.info(f"[Missing File Cleanup] Deleting item {item_identifier} (ID: {item['id']}, File: {item['filled_by_file']}) as another collected version ('{current_version}') exists.")
                                conn.execute('DELETE FROM media_items WHERE id = ?', (item['id'],))
                            else:
                                logging.info(f"[Missing File Cleanup] File missing for {item_identifier} (ID: {item['id']}, File: {item['filled_by_file']}). No other matching version found. Moving to 'Wanted'.")
                                conn.execute('''
                                    UPDATE media_items 
                                    SET state = 'Wanted', 
                                        filled_by_file = NULL, 
                                        filled_by_title = NULL, 
                                        filled_by_magnet = NULL, 
                                        filled_by_torrent_id = NULL, 
                                        collected_at = NULL,
                                        last_updated = ?,
                                        version = TRIM(version, '*') 
                                    WHERE id = ?
                                ''', (datetime.now(), item['id']))
                        except Exception as e:
                            # conn.rollback() # Rollback for THIS item was removed, transaction handles overall
                            logging.error(f"Error handling missing file for item {item_identifier} (ID: {item['id']}): {str(e)}", exc_info=True)
                    else: # rescrape_missing_files is False
                        logging.info(f"[Missing File Cleanup] File missing for {item_identifier} (ID: {item['id']}, File: {item['filled_by_file']}). 'rescrape_missing_files' is False. Deleting item.")
                        conn.execute('''
                            DELETE FROM media_items
                            WHERE id = ?
                        ''', (item['id'],))
            cursor.close()
            # logging.info(f"Finished post-loop cleanup in {time.time() - start_cleanup_time:.4f} seconds.")

        conn.commit()
    except Exception as e:
        logging.error(f"Error adding collected items: {str(e)}", exc_info=True)
        conn.rollback()
        raise
    finally:
        conn.close()

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
        conn.execute('BEGIN TRANSACTION')
        
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
                    AND version = ? 
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
                    AND version = ? 
                    AND state = 'Collected'
                    AND type = 'movie'
                '''
            
            cursor.execute(query, query_params)
            existing_item = cursor.fetchone()
            if existing_item:
                item_desc = f"S{item.get('season_number')}E{item.get('episode_number')}" if item.get('type') == 'episode' else "movie"
                logging.info(f"Item already exists ({item_desc}) with version {found_version} (from {filename_source}): {filename}")
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

def remove_original_item_from_plex(item: Dict[str, Any]):
    from utilities.plex_functions import remove_file_from_plex

    item_identifier = f"{item['type']}_{item['title']}_{item['imdb_id']}"
    original_file_path = item.get('upgrading_from')
    original_title = item.get('title')

    if original_file_path and original_title:
        success = remove_file_from_plex(original_title, original_file_path)
        if not success:
            logging.error(f"Failed to remove file from Plex: {item_identifier}")
    else:
        logging.warning(f"No file path or title found for item: {item_identifier}")

def remove_original_item_from_account(item: Dict[str, Any]):
    from queues.adding_queue import AddingQueue
    original_torrent_id = item.get('filled_by_torrent_id')

    if original_torrent_id:
        adding_queue = AddingQueue()
        adding_queue.remove_unwanted_torrent(original_torrent_id)

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
        cursor.execute("""
            INSERT INTO tv_shows (
                imdb_id, tmdb_id, title, year, status, is_complete,
                total_episodes, last_status_check, added_at, last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(imdb_id) DO UPDATE SET
                tmdb_id = COALESCE(excluded.tmdb_id, tmdb_id),
                title = COALESCE(excluded.title, title),
                year = COALESCE(excluded.year, year),
                status = COALESCE(excluded.status, status),
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
