from .core import get_db_connection, row_to_dict, normalize_string, get_existing_airtime
from metadata.metadata import get_show_airtime_by_imdb_id
import logging
import os
from datetime import datetime
import json
from .database_writing import add_to_collected_notifications
from reverse_parser import parser_approximation
from settings import get_setting

def add_collected_items(media_items_batch, recent=False):
    from routes.debug_routes import move_item_to_wanted

    conn = get_db_connection()
    try:
        conn.execute('BEGIN TRANSACTION')
        
        # Fetch all existing collected items from the database
        existing_items = conn.execute('''
            SELECT id, imdb_id, tmdb_id, title, type, season_number, episode_number, state, version, filled_by_file, release_date 
            FROM media_items 
            WHERE state IN ('Collected', 'Checking', 'Upgrading')
        ''').fetchall()
        
        # Create a set of existing filenames in 'Collected' state
        existing_collected_files = {row['filled_by_file'] for row in existing_items if row['state'] == 'Collected'}

        # Create a dictionary of existing filenames to item details for all fetched items
        existing_file_map = {row['filled_by_file']: row_to_dict(row) for row in existing_items if row['filled_by_file']}

        # Filter out items that are already in 'Collected' state
        filtered_media_items_batch = []
        for item in media_items_batch:
            locations = item.get('location', [])
            if isinstance(locations, str):
                locations = [locations]
            
            new_locations = []
            for location in locations:
                filename = os.path.basename(location)
                if filename and filename not in existing_collected_files:
                    new_locations.append(location)
            
            if new_locations:
                item['location'] = new_locations
                filtered_media_items_batch.append(item)

        # Process all items, including existing ones
        all_valid_filenames = set()
        airtime_cache = {}
        
        for item in media_items_batch:
            try:
                locations = item.get('location', [])
                if isinstance(locations, str):
                    locations = [locations]
                
                for location in locations:
                    filename = os.path.basename(location)
                    if filename:
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
                        
                        # Check if the item is currently in "Checking" state
                        if existing_item['state'] == 'Checking':
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

                            # Update the existing item
                            conn.execute('''
                                UPDATE media_items
                                SET state = ?, last_updated = ?, collected_at = ?, original_collected_at = ?
                                WHERE id = ?
                            ''', (new_state, datetime.now(), collected_at, collected_at, item_id))
                            logging.info(f"Updated existing item from Checking to {new_state}: {normalized_title} (ID: {item_id})")

                            # Fetch the updated item
                            updated_item = conn.execute('SELECT * FROM media_items WHERE id = ?', (item_id,)).fetchone()
                            
                            # Add notification for collected item
                            add_to_collected_notifications(dict(updated_item))
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

                        # Insert new item
                        if item_type == 'movie':
                            cursor = conn.execute('''
                                INSERT OR REPLACE INTO media_items
                                (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated, metadata_updated, version, collected_at, genres, filled_by_file, runtime)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ''', (
                                imdb_id, tmdb_id, normalized_title, item.get('year'),
                                item.get('release_date'), 'Collected', 'movie',
                                datetime.now(), datetime.now(), version, collected_at, genres, filename, item.get('runtime')
                            ))
                        else:
                            cursor = conn.execute('''
                                INSERT OR REPLACE INTO media_items
                                (imdb_id, tmdb_id, title, year, release_date, state, type, season_number, episode_number, episode_title, last_updated, metadata_updated, version, airtime, collected_at, genres, filled_by_file, runtime)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ''', (
                                imdb_id, tmdb_id, normalized_title, item.get('year'),
                                item.get('release_date'), 'Collected', 'episode',
                                item['season_number'], item['episode_number'], item.get('episode_title', ''),
                                datetime.now(), datetime.now(), version, item.get('airtime'), collected_at, genres, filename, item.get('runtime')
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
                                    WHERE imdb_id = ? AND type = 'movie' AND state = 'Collected'
                                ''', (item['imdb_id'],)).fetchall()
                            else:
                                matching_items = conn.execute('''
                                    SELECT id, version FROM media_items 
                                    WHERE imdb_id = ? AND type = 'episode' AND season_number = ? AND episode_number = ? AND state = 'Collected'
                                ''', (item['imdb_id'], item['season_number'], item['episode_number'])).fetchall()
                            
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