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
        
        # Fetch all existing collected items from the database
        existing_items = conn.execute('''
            SELECT id, imdb_id, tmdb_id, title, type, season_number, episode_number, state, version, filled_by_file, collected_at, release_date, upgrading_from
            FROM media_items 
            WHERE state IN ('Collected', 'Checking', 'Upgrading')
        ''').fetchall()
        
        # Create a set of existing filenames in 'Collected' state
        existing_collected_files = {row['filled_by_file'] for row in existing_items if row['state'] == 'Collected'}

        # Create a set of filenames that are being upgraded from
        upgrading_from_files = {os.path.basename(row['upgrading_from']) for row in existing_items if row['upgrading_from']}

        # Create a dictionary of existing filenames to item details for all fetched items
        existing_file_map = {row['filled_by_file']: row_to_dict(row) for row in existing_items if row['filled_by_file']}

        # Create a set to store filtered out filenames
        filtered_out_files = set()

        # Filter out items that are already in 'Collected' state or are being upgraded from
        filtered_media_items_batch = []
        for item in media_items_batch:
            locations = item.get('location', [])
            if isinstance(locations, str):
                locations = [locations]
            
            new_locations = []
            for location in locations:
                filename = os.path.basename(location)
                if filename and filename not in existing_collected_files and filename not in upgrading_from_files:
                    new_locations.append(location)
                else:
                    if filename in existing_collected_files:
                        logging.info(f"Filtered out: {item.get('title', 'Unknown')} - {filename} (already collected)")
                    elif filename in upgrading_from_files:
                        logging.info(f"Filtered out: {item.get('title', 'Unknown')} - {filename} (being upgraded from)")
                    filtered_out_files.add(filename)
            
            if new_locations:
                item['location'] = new_locations
                filtered_media_items_batch.append(item)
            else:
                logging.info(f"Completely filtered out: {item.get('title', 'Unknown')} (all locations already collected or being upgraded from)")

        # Process all items, including existing ones
        all_valid_filenames = set()
        airtime_cache = {}
        
        for item in filtered_media_items_batch:
            try:
                locations = item.get('location', [])
                if isinstance(locations, str):
                    locations = [locations]
                
                for location in locations:
                    filename = os.path.basename(location)
                    if filename and filename not in filtered_out_files:
                        all_valid_filenames.add(filename)
                        
                imdb_id = item.get('imdb_id') or None
                tmdb_id = item.get('tmdb_id')
                normalized_title = normalize_string(item.get('title', 'Unknown'))
                item_type = 'episode' if 'season_number' in item and 'episode_number' in item else 'movie'

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
                        
                        if existing_item['state'] == 'Checking' or existing_item['state'] == 'Unreleased':
                            logging.info(f"Existing item in Checking state: {normalized_title} (ID: {item_id})")
                            logging.info(f"Release date: {existing_item['release_date']}")
                            # Check if the release date is within the past 7 days
                            release_date = datetime.strptime(existing_item['release_date'], '%Y-%m-%d').date()
                            days_since_release = (datetime.now().date() - release_date).days

                            if days_since_release <= 7:
                                if get_setting("Scraping", "enable_upgrading", default=False): 
                                    new_state = 'Upgrading'
                                else:
                                    new_state = 'Collected'
                            else:
                                new_state = 'Collected'

                            # Determine if this is an upgrade or initial collection
                            is_upgrade = existing_item.get('collected_at') is not None

                            logging.info(f"existing_item: {existing_item}")
                            logging.info(f"collected_at: {existing_item.get('collected_at')}")
                            logging.info(f"is_upgrade: {is_upgrade}")
                                                    
                            if is_upgrade and get_setting("Scraping", "enable_upgrading_cleanup", default=False):
                                # Create a new dictionary with all the necessary information
                                upgrade_item = {
                                    'type': existing_item['type'],
                                    'title': existing_item['title'],
                                    'imdb_id': existing_item['imdb_id'],
                                    'upgrading_from': existing_item['upgrading_from'],
                                    'filled_by_torrent_id': existing_item.get('filled_by_torrent_id'),
                                    'version': existing_item['version']
                                }
                                remove_original_item_from_plex(upgrade_item)
                                remove_original_item_from_account(upgrade_item)
                                remove_original_item_from_results(upgrade_item, media_items_batch)
                                log_successful_upgrade(upgrade_item)

                            # Before the UPDATE statement, ensure `collected_at` is set
                            if existing_item.get('collected_at') is None:
                                existing_collected_at = collected_at
                            else:
                                existing_collected_at = existing_item.get('collected_at')

                            # Update the existing item
                            conn.execute('''
                                UPDATE media_items
                                SET state = ?, last_updated = ?, collected_at = ?, 
                                    original_collected_at = COALESCE(original_collected_at, ?)
                                WHERE id = ?
                            ''', (new_state, datetime.now(), collected_at, existing_collected_at, item_id))
                            
                            logging.info(f"Updated existing item from Checking to {new_state}: {normalized_title} (ID: {item_id})")

                            # Fetch the updated item
                            updated_item = conn.execute('SELECT * FROM media_items WHERE id = ?', (item_id,)).fetchone()
                            
                            # Add notification for collected/upgraded item
                            updated_item_dict = dict(updated_item)
                            updated_item_dict['is_upgrade'] = is_upgrade
                            # Ensure original_collected_at is always set
                            updated_item_dict['original_collected_at'] = updated_item_dict.get('original_collected_at', existing_item.get('collected_at', collected_at))
                            add_to_collected_notifications(updated_item_dict)
                        else:
                            # If it's not in "Checking" state, just update without adding a notification
                            conn.execute('''
                                UPDATE media_items
                                SET last_updated = ?, collected_at = ?
                                WHERE id = ?
                            ''', (datetime.now(), collected_at, item_id))
                            logging.debug(f"Updated existing Collected item: {normalized_title} (ID: {item_id})")

                    else:

                        # Use the parser_approximation function
                        parsed_info = parser_approximation(filename)
                        version = parsed_info['version']

                        if item_type == 'movie':
                            # For movies
                            cursor = conn.execute('''
                                INSERT OR REPLACE INTO media_items
                                (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated, metadata_updated, version, collected_at, original_collected_at, genres, filled_by_file, runtime)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ''', (
                                imdb_id, tmdb_id, normalized_title, item.get('year'),
                                item.get('release_date'), 'Collected', 'movie',
                                datetime.now(), datetime.now(), version, collected_at, collected_at, genres, filename, item.get('runtime')
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
                            cursor = conn.execute('''
                                INSERT OR REPLACE INTO media_items
                                (imdb_id, tmdb_id, title, year, release_date, state, type, season_number, episode_number, episode_title, last_updated, metadata_updated, version, airtime, collected_at, original_collected_at, genres, filled_by_file, runtime)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ''', (
                                imdb_id, tmdb_id, normalized_title, item.get('year'),
                                item.get('release_date'), 'Collected', 'episode',
                                item['season_number'], item['episode_number'], item.get('episode_title', ''),
                                datetime.now(), datetime.now(), version, airtime, collected_at, collected_at, genres, filename, item.get('runtime')
                            ))
                        logging.info(f"Added new item as Collected: {normalized_title} (ID: {cursor.lastrowid})")

            except Exception as e:
                logging.error(f"Error processing item {item.get('title', 'Unknown')}: {str(e)}", exc_info=True)
                # Continue processing other items

        # Handle items not in the batch if not recent
        if not recent:
            for row in existing_items:
                item = row_to_dict(row)
                if item['filled_by_file'] and item['filled_by_file'] not in all_valid_filenames:
                    if get_setting("Debug", "rescrape_missing_files", default=False):
                        # TODO: Implement rescrape logic to rescrape based on current content sources
                        # TODO: Should add an option to mark items as deleted to prevent cli_debrid from re-adding if deleted from Plex
                        try:
                            # Check for matching items
                            if item['type'] == 'movie':
                                matching_items = conn.execute('''
                                    SELECT id, version FROM media_items 
                                    WHERE (imdb_id = ? OR tmdb_id = ?) AND type = 'movie' AND state = 'Collected'
                                ''', (item['imdb_id'], item['tmdb_id'])).fetchall()
                            else:
                                matching_items = conn.execute('''
                                    SELECT id, version FROM media_items 
                                    WHERE (imdb_id = ? OR tmdb_id = ?) AND type = 'episode' AND season_number = ? AND episode_number = ? AND state = 'Collected'
                                ''', (item['imdb_id'], item['tmdb_id'], item['season_number'], item['episode_number'])).fetchall()
                            
                            current_version = item['version'].strip('*')
                            matching_version_exists = any(current_version == m['version'].strip('*') for m in matching_items)
                            
                            if matching_version_exists:
                                # Delete the item if a matching version exists
                                conn.execute('DELETE FROM media_items WHERE id = ?', (item['id'],))
                                logging.info(f"Deleted item (ID: {item['id']}) as matching version exists")
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
                                logging.info(f"Moved item to Wanted state (no matching version): {item['title']} (ID: {item['id']})")
                        except Exception as e:
                            conn.rollback()
                            raise e
                    else:
                        cursor = conn.execute('''
                            DELETE FROM media_items
                            WHERE id = ?
                        ''', (item['id'],))
                        logging.info(f"Deleted item (ID: {item['id']}) as file no longer present")

        conn.commit()
        logging.debug(f"Collected items processed and database updated. Original batch: {len(media_items_batch)}, Filtered batch: {len(filtered_media_items_batch)}")
    except Exception as e:
        logging.error(f"Error adding collected items: {str(e)}", exc_info=True)
        conn.rollback()
        raise
    finally:
        conn.close()

def remove_original_item_from_plex(item: Dict[str, Any]):
    from utilities.plex_functions import remove_file_from_plex

    item_identifier = f"{item['type']}_{item['title']}_{item['imdb_id']}"
    logging.info(f"Removing original file from Plex: {item_identifier}")

    original_file_path = item.get('upgrading_from')
    original_title = item.get('title')
    logging.info(f"Original file path: {original_file_path}")
    logging.info(f"Original title: {original_title}")

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
        logging.warning(f"No original torrent ID found for {item_identifier}")

def remove_original_item_from_cli_debrid(item: Dict[str, Any]):
    item_identifier = generate_identifier(item)
    logging.info(f"Removing original item from cli_debrid database: {item_identifier}")

    original_file_path = item.get('filled_by_file')
    if original_file_path:
        conn = get_db_connection()
        try:
            cursor = conn.execute('''
                DELETE FROM media_items
                WHERE filled_by_file = ?
            ''', (original_file_path,))
            if cursor.rowcount > 0:
                logging.info(f"Removed {cursor.rowcount} item(s) with filled_by_file {original_file_path} from cli_debrid database")
            else:
                logging.info(f"No items found with filled_by_file {original_file_path} in cli_debrid database")
            conn.commit()
        except Exception as e:
            logging.error(f"Error removing item from cli_debrid database: {str(e)}", exc_info=True)
            conn.rollback()
        finally:
            conn.close()
    else:
        logging.warning(f"No original file path found for {item_identifier}")

def remove_original_item_from_results(item: Dict[str, Any], media_items_batch: List[Dict[str, Any]]):
    try:
        item_identifier = generate_identifier(item)
        logging.info(f"Removing original item from results: {item_identifier}")

        original_file_path = item.get('upgrading_from')
        logging.info(f"original_file_path: {original_file_path}")
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



def generate_identifier(item: Dict[str, Any]) -> str:
    if item.get('type') == 'movie':
        return f"{item.get('title')} ({item.get('year')})"
    else:
        season = item.get('season_number', '00')
        episode = item.get('episode_number', '00')
        
        # Convert to int if possible, otherwise use string formatting
        try:
            season = f"{int(season):02d}"
        except ValueError:
            season = str(season).zfill(2)
        
        try:
            episode = f"{int(episode):02d}"
        except ValueError:
            episode = str(episode).zfill(2)
        
        return f"{item.get('title')} S{season}E{episode}"
