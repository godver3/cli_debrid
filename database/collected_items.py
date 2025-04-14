from .core import get_db_connection, row_to_dict, normalize_string, get_existing_airtime, retry_on_db_lock
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

@retry_on_db_lock()
def add_collected_items(media_items_batch, recent=False):
    from routes.debug_routes import move_item_to_wanted
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
        # --- Part 1: Read existing data (outside main batch loop/transaction) ---
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
            db_read_batch_size = 450  # For reading existing items
            filenames_list = list(filenames_in_batch)
            existing_items = []

            for i in range(0, len(filenames_list), db_read_batch_size):
                batch = filenames_list[i:i + db_read_batch_size]
                placeholders = ', '.join(['?'] * len(batch))
                query = f'''
                    SELECT id, imdb_id, tmdb_id, title, type, season_number, episode_number, state, version,
                           filled_by_file, collected_at, release_date, upgrading_from, content_source
                    FROM media_items
                    WHERE filled_by_file IN ({placeholders})
                       OR upgrading_from IN ({placeholders})
                '''
                params = batch * 2
                cursor = conn.execute(query, params)
                existing_items.extend(cursor.fetchall())
                cursor.close()

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

        # --- Part 2: Filter incoming batch ---
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

        # --- Part 3: Process filtered batch with smaller transactions ---
        batch_size = 100 # Define how many items to process before committing
        items_processed_in_current_batch = 0
        items_to_update = [] # (state, last_updated, collected_at, original_collected_at, location_on_disk, is_upgrade, resolution, item_id)
        items_to_insert_movie = [] # (imdb_id, tmdb_id, title, ...)
        items_to_insert_episode = [] # (imdb_id, tmdb_id, title, ...)
        items_to_delete = [] # (item_id,)
        items_for_notification = []
        items_for_post_processing = []
        items_marked_upgraded = [] # (item_id,)
        all_valid_filenames = set() # Keep track for final cleanup check
        airtime_cache = {} # Populate airtime cache outside batch loop if possible, or cache results within

        # --- Pre-fetch Airtime Logic (Keep as is, happens before batching loop) ---
        new_episode_show_ids = set()
        # ... (rest of the airtime pre-fetch logic using conn for reads) ...
        # ... Ensure cursor.close() is called after reads ...
        if new_episode_show_ids:
            logging.info(f"Found {len(new_episode_show_ids)} unique show IDs potentially requiring airtime check for new episodes.")
            # ... (Airtime pre-fetch logic remains the same) ...
            logging.info(f"Airtime cache populated for {len(airtime_cache)} shows.")
        # --- End Pre-fetch Airtime Logic ---


        for index, item in enumerate(filtered_media_items_batch):
            item_identifier = generate_identifier(item)
            try:
                # Keep track of filenames encountered in this specific item
                item_filenames = set()
                locations = item.get('location', [])
                if isinstance(locations, str):
                    locations = [locations]

                for location in locations:
                    filename = os.path.basename(location)
                    if filename and filename not in filtered_out_files:
                        item_filenames.add(filename)
                        all_valid_filenames.add(filename) # Add to overall set for later check

                imdb_id = item.get('imdb_id') or None
                tmdb_id = item.get('tmdb_id') or None
                normalized_title = normalize_string(item.get('title', 'Unknown'))
                item_type = 'episode' if 'season_number' in item and 'episode_number' in item else 'movie'

                if imdb_id is None and tmdb_id is None:
                    logging.warning(f"Skipping unmatched item: {item.get('title', 'Unknown')}")
                    continue

                # Use the first valid filename found for this item for DB operations
                # (Assuming one collected item corresponds to one DB row, even if multiple locations exist)
                filename_for_db = next(iter(item_filenames), None)
                if not filename_for_db:
                    logging.warning(f"No valid filename found for item {item_identifier}, skipping DB interaction.")
                    continue

                collected_at = item.get('addedAt')
                if collected_at is not None:
                    collected_at = datetime.fromtimestamp(collected_at)
                else:
                    collected_at = datetime.now()
                genres = json.dumps(item.get('genres', []))
                location_on_disk = locations[0] if locations else None # Use first location

                # Check if file exists in our pre-fetched map
                if filename_for_db in existing_file_map:
                    existing_item = existing_file_map[filename_for_db]
                    item_id = existing_item['id']

                    if existing_item['state'] not in ['Collected', 'Upgrading']:
                        if existing_item['release_date'] in ['Unknown', 'unknown', 'None', 'none', None, '']:
                            days_since_release = 0
                        else:
                            try:
                                release_date = datetime.strptime(existing_item['release_date'], '%Y-%m-%d').date()
                                days_since_release = (datetime.now().date() - release_date).days
                            except ValueError:
                                days_since_release = 0

                        is_manually_assigned = existing_item.get('content_source') == 'Magnet_Assigner'
                        should_upgrade_state = (days_since_release <= 7 and
                                          get_setting("Scraping", "enable_upgrading", default=False) and
                                          not is_manually_assigned)
                        new_state = 'Upgrading' if should_upgrade_state else 'Collected'
                        is_upgrade_event = existing_item.get('collected_at') is not None
                        # --- Queue Upgrade Cleanup ---
                        if is_upgrade_event and get_setting("Scraping", "enable_upgrading_cleanup", default=False):
                             upgrade_item_info = { # Store info needed for cleanup later
                                'type': existing_item['type'],
                                'title': existing_item['title'],
                                'imdb_id': existing_item['imdb_id'],
                                'upgrading_from': existing_item['upgrading_from'],
                                'filled_by_torrent_id': existing_item.get('filled_by_torrent_id'),
                                'version': existing_item['version'],
                                'season_number': existing_item.get('season_number'),
                                'episode_number': existing_item.get('episode_number'),
                                'filled_by_file': existing_item.get('filled_by_file'),
                                'resolution': existing_item.get('resolution')
                            }
                             if upgrade_item_info['filled_by_file'] != upgrade_item_info['upgrading_from']:
                                 # Mark the item for upgrade in DB within the batch
                                 items_marked_upgraded.append((item_id,))
                                 # Queue the actual cleanup actions (Plex, Account, Results)
                                 # These might need to happen *after* the commit or be handled differently
                                 # For now, let's just log that cleanup would happen
                                 logging.info(f"Queueing upgrade cleanup for item ID {item_id}")
                                 # remove_original_item_from_plex(upgrade_item_info) # Defer external actions
                                 # remove_original_item_from_account(upgrade_item_info) # Defer external actions
                                 # remove_original_item_from_results(upgrade_item_info, media_items_batch) # Defer external actions
                                 log_successful_upgrade(upgrade_item_info) # This might be okay if it's just logging

                        existing_collected_at_val = existing_item.get('collected_at') or collected_at
                        # --- Queue Update ---
                        items_to_update.append((
                            new_state, datetime.now(), collected_at,
                            existing_collected_at_val, # original_collected_at
                            location_on_disk,
                            is_upgrade_event, # upgraded flag
                            item.get('resolution'),
                            item_id
                        ))
                        items_processed_in_current_batch += 1

                        # --- Queue Post Processing / Notifications ---
                        # Need item state *after* update, so fetch after commit or estimate
                        # For simplicity, let's assume the state is new_state
                        if not existing_item.get('collected_at'): # If it was previously not collected
                             notification_state = 'Upgraded' if is_upgrade_event else 'Collected'
                             items_for_notification.append({
                                 'id': item_id, # Need ID to fetch full item later
                                 'new_state': notification_state,
                                 'is_upgrade': is_upgrade_event,
                                 'original_collected_at': existing_item.get('collected_at', collected_at)
                             })
                        # Queue for handle_state_change post-processing
                        items_for_post_processing.append({'id': item_id}) # Need ID to fetch full item later

                # Logic for inserting new items
                else:
                    parsed_info = parser_approximation(filename_for_db)
                    version = parsed_info['version']

                    if item_type == 'movie':
                        # --- Queue Insert Movie ---
                        items_to_insert_movie.append((
                            imdb_id, tmdb_id, normalized_title, item.get('year'),
                            item.get('release_date'), 'Collected', 'movie',
                            datetime.now(), datetime.now(), version, collected_at, collected_at, genres,
                            filename_for_db, item.get('runtime'), location_on_disk, False, # upgraded=False
                            item.get('country', '').lower(), item.get('resolution'), item.get('physical_release_date')
                        ))
                    else: # Episode
                        # Get airtime (use cache populated earlier)
                        airtime = airtime_cache.get(imdb_id, '19:00') # Default if missing
                        # --- Queue Insert Episode ---
                        items_to_insert_episode.append((
                            imdb_id, tmdb_id, normalized_title, item.get('year'),
                            item.get('release_date'), 'Collected', 'episode',
                            item['season_number'], item['episode_number'], item.get('episode_title', ''),
                            datetime.now(), datetime.now(), version, airtime, collected_at, collected_at, genres,
                            filename_for_db, item.get('runtime'), location_on_disk, False, # upgraded=False
                            item.get('country', '').lower(), item.get('resolution')
                        ))
                    items_processed_in_current_batch += 1

            except Exception as e:
                logging.error(f"Error preparing item {item_identifier} for batch: {str(e)}", exc_info=True)
                # Decide if you want to skip this item or halt the process
                continue # Skip this item on error

            # --- Commit Batch ---
            is_last_item = (index == len(filtered_media_items_batch) - 1)
            if items_processed_in_current_batch >= batch_size or (is_last_item and items_processed_in_current_batch > 0) :
                try:
                    conn.execute('BEGIN TRANSACTION')
                    logging.debug(f"Committing batch of {items_processed_in_current_batch} items...")

                    if items_to_update:
                        conn.executemany('''
                            UPDATE media_items
                            SET state = ?, last_updated = ?, collected_at = ?,
                                original_collected_at = COALESCE(original_collected_at, ?),
                                location_on_disk = ?, upgraded = ?, resolution = ?
                            WHERE id = ?
                        ''', items_to_update)
                        logging.debug(f"Executed {len(items_to_update)} updates.")

                    if items_marked_upgraded:
                         conn.executemany('''
                            UPDATE media_items SET upgraded = 1 WHERE id = ?
                         ''', items_marked_upgraded)
                         logging.debug(f"Marked {len(items_marked_upgraded)} items as upgraded.")

                    if items_to_insert_movie:
                        conn.executemany('''
                            INSERT INTO media_items
                            (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated, metadata_updated, version, collected_at, original_collected_at, genres, filled_by_file, runtime, location_on_disk, upgraded, country, resolution, physical_release_date)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ''', items_to_insert_movie)
                        logging.debug(f"Executed {len(items_to_insert_movie)} movie inserts.")

                    if items_to_insert_episode:
                        conn.executemany('''
                            INSERT INTO media_items
                            (imdb_id, tmdb_id, title, year, release_date, state, type, season_number, episode_number, episode_title, last_updated, metadata_updated, version, airtime, collected_at, original_collected_at, genres, filled_by_file, runtime, location_on_disk, upgraded, country, resolution)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ''', items_to_insert_episode)
                        logging.debug(f"Executed {len(items_to_insert_episode)} episode inserts.")

                    # Commit the DB changes for the batch
                    conn.commit()
                    logging.info(f"Successfully committed batch of {items_processed_in_current_batch} items.")

                    # --- Post-Commit Actions (Notifications, State Changes) ---
                    # Fetch full data for items needing post-processing now that commit is done
                    ids_for_post_processing = [p['id'] for p in items_for_post_processing]
                    if ids_for_post_processing:
                         placeholders = ', '.join(['?'] * len(ids_for_post_processing))
                         cursor = conn.execute(f'SELECT * FROM media_items WHERE id IN ({placeholders})', ids_for_post_processing)
                         post_process_items_data = {row['id']: dict(row) for row in cursor.fetchall()}
                         cursor.close()
                         for item_info in items_for_post_processing:
                             item_data = post_process_items_data.get(item_info['id'])
                             if item_data:
                                 handle_state_change(item_data) # Call the post-processing handler

                    # Handle notifications
                    ids_for_notification = [n['id'] for n in items_for_notification]
                    if ids_for_notification:
                         placeholders = ', '.join(['?'] * len(ids_for_notification))
                         cursor = conn.execute(f'SELECT * FROM media_items WHERE id IN ({placeholders})', ids_for_notification)
                         notification_items_data = {row['id']: dict(row) for row in cursor.fetchall()}
                         cursor.close()
                         for item_info in items_for_notification:
                             item_data = notification_items_data.get(item_info['id'])
                             if item_data:
                                 # Add the extra info needed for notification
                                 item_data['new_state'] = item_info['new_state']
                                 item_data['is_upgrade'] = item_info['is_upgrade']
                                 item_data['original_collected_at'] = item_info['original_collected_at']
                                 add_to_collected_notifications(item_data)


                    # Clear lists for the next batch
                    items_processed_in_current_batch = 0
                    items_to_update = []
                    items_to_insert_movie = []
                    items_to_insert_episode = []
                    items_to_delete = []
                    items_for_notification = []
                    items_for_post_processing = []
                    items_marked_upgraded = []

                except Exception as batch_ex:
                    logging.error(f"Error committing batch: {batch_ex}", exc_info=True)
                    conn.rollback()
                    # Decide: stop processing? Skip batch? Log and continue?
                    # For now, let's log and continue with the next batch.
                    # Clear lists as the rollback undid the changes for this batch
                    items_processed_in_current_batch = 0
                    items_to_update = []
                    items_to_insert_movie = []
                    items_to_insert_episode = []
                    items_to_delete = []
                    items_for_notification = []
                    items_for_post_processing = []
                    items_marked_upgraded = []
                    continue # Move to the next item


        # --- Part 4: Final Cleanup Check (Missing Files) ---
        if not recent:
            logging.info("Performing final check for missing files among collected items...")
            batch_size_cleanup = 500 # Batch size for the cleanup delete/update phase
            items_to_set_wanted = [] # (last_updated, version, item_id)
            items_to_delete_missing = [] # (item_id,)

            # Fetch all 'Collected' items efficiently
            all_collected_items = []
            cursor = conn.execute("SELECT id, imdb_id, tmdb_id, title, type, season_number, episode_number, state, version, filled_by_file, collected_at, release_date, upgrading_from FROM media_items WHERE state = 'Collected'")
            all_collected_items = cursor.fetchall()
            cursor.close()

            collected_count = len(all_collected_items)
            processed_cleanup_count = 0
            logging.info(f"Checking {collected_count} collected items for missing files...")

            for item_row in all_collected_items:
                item = row_to_dict(item_row)
                item_identifier = generate_identifier(item)
                processed_cleanup_count += 1

                if item['filled_by_file'] and item['filled_by_file'] not in all_valid_filenames:
                    if get_setting("Debug", "rescrape_missing_files", default=False):
                        try:
                            # Check if another version exists (read operation, okay outside batch commit)
                            if item['type'] == 'movie':
                                matching_cursor = conn.execute('''
                                    SELECT id, version FROM media_items
                                    WHERE (imdb_id = ? OR tmdb_id = ?) AND type = 'movie' AND state = 'Collected'
                                ''', (item['imdb_id'], item['tmdb_id']))
                            else:
                                matching_cursor = conn.execute('''
                                    SELECT id, version FROM media_items
                                    WHERE (imdb_id = ? OR tmdb_id = ?) AND type = 'episode' AND season_number = ? AND episode_number = ? AND state = 'Collected'
                                ''', (item['imdb_id'], item['tmdb_id'], item['season_number'], item['episode_number']))

                            matching_items = matching_cursor.fetchall()
                            matching_cursor.close()

                            current_version = item['version'].strip('*') if item['version'] else ''
                            matching_version_exists = any(
                                (m['version'].strip('*') if m['version'] else '') == current_version
                                for m in matching_items if m['id'] != item['id']
                            )

                            if matching_version_exists:
                                items_to_delete_missing.append((item['id'],))
                            else:
                                items_to_set_wanted.append((datetime.now(), current_version, item['id'])) # Prepare update data

                        except Exception as e:
                            logging.error(f"Error checking for matching versions during cleanup for {item_identifier}: {str(e)}")
                            # Decide how to handle - maybe skip this item?
                    else:
                        items_to_delete_missing.append((item['id'],)) # Prepare delete data

                # Commit the cleanup batch periodically
                if len(items_to_set_wanted) + len(items_to_delete_missing) >= batch_size_cleanup or processed_cleanup_count == collected_count:
                    if items_to_set_wanted or items_to_delete_missing:
                        try:
                            conn.execute('BEGIN TRANSACTION')
                            if items_to_delete_missing:
                                conn.executemany('DELETE FROM media_items WHERE id = ?', items_to_delete_missing)
                                logging.debug(f"Cleanup: Deleted {len(items_to_delete_missing)} items with missing files.")
                            if items_to_set_wanted:
                                conn.executemany('''
                                    UPDATE media_items
                                    SET state = 'Wanted', filled_by_file = NULL, filled_by_title = NULL,
                                        filled_by_magnet = NULL, filled_by_torrent_id = NULL,
                                        collected_at = NULL, last_updated = ?, version = ?
                                    WHERE id = ?
                                ''', items_to_set_wanted)
                                logging.debug(f"Cleanup: Set {len(items_to_set_wanted)} items with missing files to 'Wanted'.")
                            conn.commit()
                            items_to_set_wanted = []
                            items_to_delete_missing = []
                        except Exception as cleanup_ex:
                            logging.error(f"Error committing cleanup batch: {cleanup_ex}", exc_info=True)
                            conn.rollback()
                            # Clear lists as rollback occurred
                            items_to_set_wanted = []
                            items_to_delete_missing = []

            logging.info("Finished final check for missing files.")


    except Exception as e:
        # Log the error, rollback might not be needed if errors are handled per-batch
        logging.error(f"Outer error in add_collected_items: {str(e)}", exc_info=True)
        # Rollback any potentially open transaction from the cleanup phase
        try:
            conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Error during final rollback: {rb_ex}")
        raise # Re-raise the exception after attempting rollback
    finally:
        # Ensure connection is closed
        if conn:
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

    conn = None # Initialize conn to None
    items_to_insert = []
    batch_size = 100 # Batch size for inserts

    try:
        conn = get_db_connection() # Establish connection once

        for index, item in enumerate(media_items_batch):
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

            # logging.debug(f"Using filename from {filename_source}: {filename}")
            found_version = None

            # Check if any version from the list is in the filename
            for version in version_list:
                if version.lower() in filename.lower():
                    found_version = version
                    # logging.debug(f"Found version {version} in filename")
                    break

            # If no version found, try parser_approximation
            if not found_version:
                parsed_result = parser_approximation(filename)
                found_version = parsed_result.get('version')
                # if found_version:
                    # logging.debug(f"Found version {found_version} using parser_approximation")

            if not found_version:
                logging.warning(f"Could not determine version for {filename_source}: {filename}")
                continue

            # Check if item with this version already exists (Read operation)
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
                cursor.close()
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
            cursor.close() # Close cursor after read

            if existing_item:
                # item_desc = f"S{item.get('season_number')}E{item.get('episode_number')}" if item.get('type') == 'episode' else "movie"
                # logging.info(f"Item already exists ({item_desc}) with version {found_version} (from {filename_source}): {filename}")
                continue

            # Prepare item data for insertion
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            collected_at = item.get('collected_at', now)  # Use item's collected_at if available, fallback to now

            if item.get('type') == 'episode':
                insert_data = (
                    item.get('imdb_id'), item.get('tmdb_id'), item.get('title'), item.get('type'),
                    item.get('season_number'), item.get('episode_number'), found_version, collected_at,
                    filename, item.get('year'), item.get('release_date'), now, now
                )
            else: # Movie
                 insert_data = (
                    item.get('imdb_id'), item.get('tmdb_id'), item.get('title'), item.get('type'),
                    found_version, collected_at, filename, item.get('year'),
                    item.get('release_date'), now, now
                )
            items_to_insert.append(insert_data)
            # logging.info(f"Prepared new item for collection with version {found_version} (from {filename_source}): {filename}")

            # Commit the batch if size is reached or it's the last item
            is_last_item = (index == len(media_items_batch) - 1)
            if len(items_to_insert) >= batch_size or (is_last_item and items_to_insert):
                try:
                    conn.execute('BEGIN TRANSACTION')
                    if item.get('type') == 'episode': # Need to know which type for executemany
                         conn.executemany('''
                            INSERT INTO media_items
                            (imdb_id, tmdb_id, title, type, season_number, episode_number, version, collected_at, state, filled_by_file,
                             year, release_date, last_updated, metadata_updated)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Collected', ?, ?, ?, ?, ?)
                         ''', items_to_insert)
                    else:
                         conn.executemany('''
                            INSERT INTO media_items
                            (imdb_id, tmdb_id, title, type, version, collected_at, state, filled_by_file,
                             year, release_date, last_updated, metadata_updated)
                            VALUES (?, ?, ?, ?, ?, ?, 'Collected', ?, ?, ?, ?, ?)
                         ''', items_to_insert)

                    conn.commit()
                    logging.info(f"Committed batch of {len(items_to_insert)} collected items (Plex disabled).")
                    items_to_insert = [] # Clear for next batch
                except sqlite3.Error as e:
                    logging.error(f"Database error committing batch in plex_collection_disabled: {e}")
                    conn.rollback()
                    items_to_insert = [] # Clear list after rollback
                    # Optionally re-raise or handle more gracefully
                except Exception as e_batch:
                    logging.error(f"Unexpected error committing batch in plex_collection_disabled: {e_batch}")
                    conn.rollback()
                    items_to_insert = []
                    # Optionally re-raise

        return True # Assume success unless an exception was raised and not handled

    except Exception as e:
        logging.error(f"Error in plex_collection_disabled: {str(e)}", exc_info=True)
        # Rollback might happen within batch commit, but add here for outer errors
        if conn:
            try:
                conn.rollback()
            except Exception as rb_ex:
                 logging.error(f"Error during final rollback in plex_collection_disabled: {rb_ex}")
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
