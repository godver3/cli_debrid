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
from .unmatched_helper import find_matching_item_in_db

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
                            release_date = datetime.strptime(existing_item['release_date'], '%Y-%m-%d').date()
                            days_since_release = (datetime.now().date() - release_date).days

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
                                (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated, metadata_updated, version, collected_at, original_collected_at, genres, filled_by_file, runtime, location_on_disk, upgraded, country, resolution)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ''', (
                                imdb_id, tmdb_id, normalized_title, item.get('year'),
                                item.get('release_date'), 'Collected', 'movie',
                                datetime.now(), datetime.now(), version, collected_at, collected_at, genres, filename, item.get('runtime'), location, False, item.get('country', '').lower(), item.get('resolution')
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
