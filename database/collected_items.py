from .core import get_db_connection, row_to_dict, normalize_string, get_existing_airtime
from metadata.metadata import get_show_airtime_by_imdb_id
import logging
import os
from datetime import datetime
import json
from .database_writing import add_to_collected_notifications, update_media_item_state
from reverse_parser import parser_approximation
from settings import get_setting
from typing import Dict, Any, List
from utilities.post_processing import handle_state_change

def add_collected_items(media_items_batch, recent=False):
    from routes.debug_routes import move_item_to_wanted
    from datetime import datetime, timedelta
    from settings import get_setting
    from queues.upgrading_queue import log_successful_upgrade

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
                           filled_by_file, collected_at, release_date, upgrading_from
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
        
        for item in filtered_media_items_batch:
            item_identifier = generate_identifier(item)
            try:
                locations = item.get('location', [])
                if isinstance(locations, str):
                    locations = [locations]
                
                for location in locations:
                    filename = os.path.basename(location)
                    if filename and filename not in filtered_out_files:
                        all_valid_filenames.add(filename)
                        
                imdb_id = item.get('imdb_id') or None
                tmdb_id = item.get('tmdb_id') or None
                normalized_title = normalize_string(item.get('title', 'Unknown'))
                item_type = 'episode' if 'season_number' in item and 'episode_number' in item else 'movie'

                if imdb_id is None and tmdb_id is None:
                    logging.warning(f"Skipping unmatched item: {item.get('title', 'Unknown')}")
                    continue

                for location in locations:
                    filename = os.path.basename(location)

                    added_at = item.get('addedAt')
                    if added_at is not None:
                        collected_at = datetime.fromtimestamp(added_at)
                    else:
                        collected_at = datetime.now()
                    genres = json.dumps(item.get('genres', []))

                    if filename in existing_file_map:
                        existing_item = existing_file_map[filename]
                        item_id = existing_item['id']
                        
                        if existing_item['state'] not in ['Collected', 'Upgrading']:
                            if existing_item['release_date'] in ['Unknown', 'unknown', 'None', 'none', None, '']:
                                # Treat unknown dates as new content
                                days_since_release = 0
                                logging.debug(f"Unknown release date for {item_identifier} - treating as new content")
                            else:
                                try:
                                    release_date = datetime.strptime(existing_item['release_date'], '%Y-%m-%d').date()
                                    days_since_release = (datetime.now().date() - release_date).days
                                except ValueError:
                                    # Handle invalid but non-empty release dates by treating them as new
                                    logging.debug(f"Invalid release date format: {existing_item['release_date']} - treating as new content")
                                    days_since_release = 0

                            if days_since_release <= 7:
                                if get_setting("Scraping", "enable_upgrading", default=False): 
                                    new_state = 'Upgrading'
                                else:
                                    new_state = 'Collected'
                            else:
                                new_state = 'Collected'

                            is_upgrade = existing_item.get('collected_at') is not None

                            if is_upgrade and get_setting("Scraping", "enable_upgrading_cleanup", default=False):
                                upgrade_item = {
                                    'type': existing_item['type'],
                                    'title': existing_item['title'],
                                    'imdb_id': existing_item['imdb_id'],
                                    'upgrading_from': existing_item['upgrading_from'],
                                    'filled_by_torrent_id': existing_item.get('filled_by_torrent_id'),
                                    'version': existing_item['version'],
                                    'season_number': existing_item.get('season_number'),
                                    'episode_number': existing_item.get('episode_number'),
                                    'filled_by_file': existing_item.get('filled_by_file'),
                                    'resolution': existing_item.get('resolution')  # Preserve old resolution for reference
                                }
                                
                                if upgrade_item['filled_by_file'] != upgrade_item['upgrading_from']:
                                    conn.execute('''
                                        UPDATE media_items
                                        SET upgraded = 1
                                        WHERE id = ?
                                    ''', (item_id,))
                                    
                                    remove_original_item_from_plex(upgrade_item)
                                    remove_original_item_from_account(upgrade_item)
                                    remove_original_item_from_results(upgrade_item, media_items_batch)
                                    log_successful_upgrade(upgrade_item)
                                
                            existing_collected_at = existing_item.get('collected_at') or collected_at

                            conn.execute('''
                                UPDATE media_items
                                SET state = ?, last_updated = ?, collected_at = ?, 
                                    original_collected_at = COALESCE(original_collected_at, ?),
                                    location_on_disk = ?, upgraded = ?, resolution = ?
                                WHERE id = ?
                            ''', (new_state, datetime.now(), collected_at, existing_collected_at, 
                                  location, is_upgrade, item.get('resolution'), item_id))

                            # Add post-processing call after state update
                            if new_state == 'Collected':
                                handle_state_change(dict(conn.execute('SELECT * FROM media_items WHERE id = ?', (item_id,)).fetchone()))
                            elif new_state == 'Upgrading':
                                handle_state_change(dict(conn.execute('SELECT * FROM media_items WHERE id = ?', (item_id,)).fetchone()))

                            if not existing_item.get('collected_at'):
                                cursor = conn.execute('SELECT * FROM media_items WHERE id = ?', (item_id,))
                                updated_item = cursor.fetchone()
                                cursor.close()
                                
                                updated_item_dict = dict(updated_item)
                                updated_item_dict['is_upgrade'] = is_upgrade
                                if is_upgrade:
                                    notification_state = 'Upgraded'
                                else:
                                    notification_state = 'Collected'
                                updated_item_dict['new_state'] = notification_state
                                updated_item_dict['original_collected_at'] = updated_item_dict.get('original_collected_at', existing_item.get('collected_at', collected_at))
                                add_to_collected_notifications(updated_item_dict)

                    else:
                        parsed_info = parser_approximation(filename)
                        version = parsed_info['version']

                        if item_type == 'movie':
                            conn.execute('''
                                INSERT OR REPLACE INTO media_items
                                (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated, metadata_updated, version, collected_at, original_collected_at, genres, filled_by_file, runtime, location_on_disk, upgraded, country, resolution, physical_release_date)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ''', (
                                imdb_id, tmdb_id, normalized_title, item.get('year'),
                                item.get('release_date'), 'Collected', 'movie',
                                datetime.now(), datetime.now(), version, collected_at, collected_at, genres, filename, item.get('runtime'), location, False, item.get('country', '').lower(), item.get('resolution'), item.get('physical_release_date')
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
                                datetime.now(), datetime.now(), version, airtime, collected_at, collected_at, genres, filename, item.get('runtime'), location, False, item.get('country', '').lower(), item.get('resolution')
                            ))

            except Exception as e:
                logging.error(f"Error processing item {item_identifier}: {str(e)}", exc_info=True)
                continue

        if not recent:
            cursor = conn.execute('''
                SELECT id, imdb_id, tmdb_id, title, type, season_number, episode_number, state, version, filled_by_file, collected_at, release_date, upgrading_from
                FROM media_items
                WHERE state = 'Collected'
            ''')
            for row in cursor:
                item = row_to_dict(row)
                item_identifier = generate_identifier(item)
                if item['filled_by_file'] and item['filled_by_file'] not in all_valid_filenames:
                    if get_setting("Debug", "rescrape_missing_files", default=False):
                        try:
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
                            
                            current_version = item['version'].strip('*')
                            matching_version_exists = any(current_version == m['version'].strip('*') for m in matching_items if m['id'] != item['id'])
                            
                            if matching_version_exists:
                                conn.execute('DELETE FROM media_items WHERE id = ?', (item['id'],))
                            else:
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
                            conn.rollback()
                            logging.error(f"Error handling missing file for item {item_identifier}: {str(e)}", exc_info=True)
                    else:
                        conn.execute('''
                            DELETE FROM media_items
                            WHERE id = ?
                        ''', (item['id'],))
            cursor.close()

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

    from settings import load_config
    from reverse_parser import parser_approximation
    
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
