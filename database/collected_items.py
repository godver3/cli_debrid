from .core import get_db_connection, row_to_dict, normalize_string, get_existing_airtime
from metadata.metadata import get_show_airtime_by_imdb_id
import logging
import os
from datetime import datetime
import json
from .database_writing import add_to_collected_notifications
from reverse_parser import parser_approximation
from settings import get_setting
from typing import Dict, Any, List

def add_collected_items(media_items_batch, recent=False):
    from routes.debug_routes import move_item_to_wanted
    from datetime import datetime, timedelta
    from settings import get_setting
    from queues.upgrading_queue import log_successful_upgrade

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
            # Fetch existing items that match filenames in the batch or in upgrading_from
            placeholders = ', '.join(['?'] * len(filenames_in_batch))
            query = f'''
                SELECT id, imdb_id, tmdb_id, title, type, season_number, episode_number, state, version,
                       filled_by_file, collected_at, release_date, upgrading_from
                FROM media_items
                WHERE filled_by_file IN ({placeholders})
                   OR upgrading_from IN ({placeholders})
            '''
            params = list(filenames_in_batch) * 2  # Parameters for both placeholders
            cursor = conn.execute(query, params)
            for row in cursor:
                filled_by_file = row['filled_by_file']
                upgrading_from = os.path.basename(row['upgrading_from'] or '')
                state = row['state']

                if state == 'Collected':
                    existing_collected_files.add(filled_by_file)
                if state == 'Upgrading':
                    # Add both filled_by_file and upgrading_from to the sets
                    if filled_by_file:
                        existing_collected_files.add(filled_by_file)
                    if upgrading_from:
                        upgrading_from_files.add(upgrading_from)

                # Map both filled_by_file and upgrading_from to the existing item
                if filled_by_file:
                    existing_file_map[filled_by_file] = row_to_dict(row)
                if upgrading_from:
                    existing_file_map[upgrading_from] = row_to_dict(row)
            cursor.close()

        # Create a set to store filtered out filenames
        filtered_out_files = set()

        # Filter out items that are already in 'Collected' or 'Upgrading' state
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
                        if filename in existing_collected_files:
                            logging.info(f"Filtered out: {item.get('title', 'Unknown')} - {filename} (already collected)")
                        elif filename in upgrading_from_files:
                            logging.info(f"Filtered out: {item.get('title', 'Unknown')} - {filename} (being upgraded from)")
                        filtered_out_files.add(filename)
                else:
                    new_locations.append(location)
            
            if new_locations:
                item['location'] = new_locations
                filtered_media_items_batch.append(item)
            elif recent:
                logging.info(f"Completely filtered out: {item.get('title', 'Unknown')} (all locations already collected or being upgraded from)")

        # Process all items, including existing ones
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
                    logging.warning(f"Skipping item as neither imdb_id nor tmdb_id is provided: {item_identifier}. This item has likely not been matched correctly. See item title: {item.get('title', 'Unknown')}")
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
                            # Check if the release date is within the past 7 days
                            release_date = datetime.strptime(existing_item['release_date'], '%Y-%m-%d').date()
                            days_since_release = (datetime.now().date() - release_date).days

                            logging.info(f"Existing item in Checking state: {item_identifier} (ID: {item_id}) location: {location} with release date: {release_date}")

                            if days_since_release <= 7:
                                if get_setting("Scraping", "enable_upgrading", default=False): 
                                    new_state = 'Upgrading'
                                else:
                                    new_state = 'Collected'
                            else:
                                new_state = 'Collected'

                            # Determine if this is an upgrade or initial collection
                            is_upgrade = existing_item.get('collected_at') is not None
                                                                            
                            if is_upgrade and get_setting("Scraping", "enable_upgrading_cleanup", default=False):
                                # Create a new dictionary with all the necessary information
                                upgrade_item = {
                                    'type': existing_item['type'],
                                    'title': existing_item['title'],
                                    'imdb_id': existing_item['imdb_id'],
                                    'upgrading_from': existing_item['upgrading_from'],
                                    'filled_by_torrent_id': existing_item.get('filled_by_torrent_id'),
                                    'version': existing_item['version'],
                                    'season_number': existing_item.get('season_number'),
                                    'episode_number': existing_item.get('episode_number'),
                                    'filled_by_file': existing_item.get('filled_by_file')
                                }
                                
                                # Check if it's not a "pseudo-upgrade" (different release name, same filename)
                                if upgrade_item['filled_by_file'] != upgrade_item['upgrading_from']:
                                    remove_original_item_from_plex(upgrade_item)
                                    remove_original_item_from_account(upgrade_item)
                                    remove_original_item_from_results(upgrade_item, media_items_batch)
                                    log_successful_upgrade(upgrade_item)
                                else:
                                    logging.info(f"Skipping cleanup/notification for pseudo-upgrade: {upgrade_item['title']} (same filename)")
                                
                            # Before the UPDATE statement, ensure `collected_at` is set
                            existing_collected_at = existing_item.get('collected_at') or collected_at

                            # Update the existing item
                            conn.execute('''
                                UPDATE media_items
                                SET state = ?, last_updated = ?, collected_at = ?, 
                                    original_collected_at = COALESCE(original_collected_at, ?),
                                    location_on_disk = ?, upgraded = ?
                                WHERE id = ?
                            ''', (new_state, datetime.now(), collected_at, existing_collected_at, location, is_upgrade, item_id))
                            
                            logging.info(f"Updated existing item from Checking to {new_state}: {item_identifier} (ID: {item_id})")

                            # Only create notification if this is not an upgrade of an existing item
                            if not existing_item.get('collected_at'):
                                # Fetch the updated item
                                cursor = conn.execute('SELECT * FROM media_items WHERE id = ?', (item_id,))
                                updated_item = cursor.fetchone()
                                cursor.close()
                                
                                # Add notification for collected item
                                updated_item_dict = dict(updated_item)
                                updated_item_dict['is_upgrade'] = is_upgrade
                                # Ensure original_collected_at is always set
                                updated_item_dict['original_collected_at'] = updated_item_dict.get('original_collected_at', existing_item.get('collected_at', collected_at))
                                add_to_collected_notifications(updated_item_dict)
                        else:
                            # If it's not in "Checking" state, no update needed
                            logging.debug(f"No update needed on collected item: {item_identifier} (ID: {item_id}) location: {location}")

                    else:

                        # Use the parser_approximation function
                        parsed_info = parser_approximation(filename)
                        version = parsed_info['version']

                        if item_type == 'movie':
                            # For movies
                            conn.execute('''
                                INSERT OR REPLACE INTO media_items
                                (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated, metadata_updated, version, collected_at, original_collected_at, genres, filled_by_file, runtime, location_on_disk, upgraded)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ''', (
                                imdb_id, tmdb_id, normalized_title, item.get('year'),
                                item.get('release_date'), 'Collected', 'movie',
                                datetime.now(), datetime.now(), version, collected_at, collected_at, genres, filename, item.get('runtime'), location, False
                            ))
                        else:
                            if imdb_id not in airtime_cache:
                                airtime_cache[imdb_id] = get_existing_airtime(conn, imdb_id)
                                if airtime_cache[imdb_id] is None:
                                    airtime_cache[imdb_id] = get_show_airtime_by_imdb_id(imdb_id)
                                if not airtime_cache[imdb_id]:
                                    airtime_cache[imdb_id] = '19:00'
                            
                            airtime = airtime_cache[imdb_id]
                            # For episodes
                            conn.execute('''
                                INSERT OR REPLACE INTO media_items
                                (imdb_id, tmdb_id, title, year, release_date, state, type, season_number, episode_number, episode_title, last_updated, metadata_updated, version, airtime, collected_at, original_collected_at, genres, filled_by_file, runtime, location_on_disk, upgraded)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ''', (
                                imdb_id, tmdb_id, normalized_title, item.get('year'),
                                item.get('release_date'), 'Collected', 'episode',
                                item['season_number'], item['episode_number'], item.get('episode_title', ''),
                                datetime.now(), datetime.now(), version, airtime, collected_at, collected_at, genres, filename, item.get('runtime'), location, False
                            ))
                            logging.info(f"Added new item as Collected: {item_identifier} location: {location}")

            except Exception as e:
                logging.error(f"Error processing item {item_identifier}: {str(e)}", exc_info=True)
                # Continue processing other items

        # Handle items not in the batch if not recent
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
                        # TODO: Implement rescrape logic to rescrape based on current content sources
                        # TODO: Should add an option to mark items as deleted to prevent cli_debrid from re-adding if deleted from Plex
                        try:
                            # Check for matching items
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
                                # Delete the item if a matching version exists
                                conn.execute('DELETE FROM media_items WHERE id = ?', (item['id'],))
                                logging.info(f"Deleted item {item_identifier} as matching version {item['version']} is still collected")
                            else:
                                # Move to Wanted state if no matching version exists
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
                                logging.info(f"Moved item to Wanted state as no matching version {item['version']} is collected: {item_identifier} (ID: {item['id']})")
                        except Exception as e:
                            conn.rollback()
                            logging.error(f"Error handling missing file for item {item_identifier}: {str(e)}", exc_info=True)
                    else:
                        conn.execute('''
                            DELETE FROM media_items
                            WHERE id = ?
                        ''', (item['id'],))
                        logging.info(f"Deleted item {item_identifier} as file no longer present")
            cursor.close()

        conn.commit()
        logging.debug(f"Collected items processed and database updated.")
    except Exception as e:
        logging.error(f"Error adding collected items: {str(e)}", exc_info=True)
        conn.rollback()
        raise
    finally:
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
    logging.info(f"Removing original file from Plex: {item_identifier}")

    original_file_path = item.get('upgrading_from')
    original_title = item.get('title')

    if original_file_path and original_title:
        success = remove_file_from_plex(original_title, original_file_path)
        if success:
            logging.info(f"Successfully removed file from Plex: {item_identifier}")
        else:
            logging.warning(f"Failed to remove file from Plex: {item_identifier}")
    else:
        logging.warning(f"No file path or title found for item: {item_identifier}")


def remove_original_item_from_account(item: Dict[str, Any]):
    from queues.adding_queue import AddingQueue
    item_identifier = f"{item['type']}_{item['title']}_{item['imdb_id']}"
    logging.info(f"Removing original item from account: {item_identifier}")

    original_torrent_id = item.get('filled_by_torrent_id')

    if original_torrent_id:
        adding_queue = AddingQueue()
        adding_queue.remove_unwanted_torrent(original_torrent_id)
        logging.info(f"Removed original torrent with ID {original_torrent_id} from account")
    else:
        logging.info(f"No original torrent ID found for {item_identifier} (this is expected if the item was successfully removed from Plex)")

def remove_original_item_from_results(item: Dict[str, Any], media_items_batch: List[Dict[str, Any]]):
    try:
        item_identifier = generate_identifier(item)
        logging.info(f"Removing original item from results: {item_identifier}")

        original_file_path = item.get('upgrading_from')
        if original_file_path:
            original_filename = os.path.basename(original_file_path)
            media_items_batch[:] = [batch_item for batch_item in media_items_batch 
                                    if not any(os.path.basename(loc) == original_filename 
                                               for loc in batch_item.get('location', [])
                                               if isinstance(loc, str))]
            logging.info(f"Removed items with filename {original_filename} from media_items_batch")
        else:
            logging.warning(f"No original file path found for {item_identifier}")
    except Exception as e:
        logging.error(f"Error in remove_original_item_from_results: {str(e)}", exc_info=True)
