from .core import get_db_connection, row_to_dict, normalize_string, get_existing_airtime
from metadata.metadata import get_show_airtime_by_imdb_id
import logging
import os
from datetime import datetime
import json

def add_collected_items(media_items_batch, recent=False):
    conn = get_db_connection()
    try:
        conn.execute('BEGIN TRANSACTION')
        
        # Fetch all existing collected items from the database
        existing_items = conn.execute('''
            SELECT id, imdb_id, tmdb_id, title, type, season_number, episode_number, state, version, filled_by_file 
            FROM media_items 
            WHERE state IN ('Collected', 'Checking')
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
                        
                        # Update the existing item
                        conn.execute('''
                            UPDATE media_items
                            SET state = ?, last_updated = ?, collected_at = ?
                            WHERE id = ?
                        ''', ('Collected', datetime.now(), collected_at, item_id))
                        logging.info(f"Updated existing item to Collected: {normalized_title} (ID: {item_id})")
                    else:
                        # Insert new item
                        if item_type == 'movie':
                            cursor = conn.execute('''
                                INSERT OR REPLACE INTO media_items
                                (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated, metadata_updated, version, collected_at, genres, filled_by_file)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ''', (
                                imdb_id, tmdb_id, normalized_title, item.get('year'),
                                item.get('release_date'), 'Collected', 'movie',
                                datetime.now(), datetime.now(), 'unknown', collected_at, genres, filename
                            ))
                        else:
                            if imdb_id not in airtime_cache:
                                airtime_cache[imdb_id] = get_existing_airtime(conn, imdb_id)
                                if airtime_cache[imdb_id] is None:
                                    airtime_cache[imdb_id] = get_show_airtime_by_imdb_id(imdb_id)
                                if not airtime_cache[imdb_id]:
                                    airtime_cache[imdb_id] = '19:00'
                            
                            airtime = airtime_cache[imdb_id]
                            cursor = conn.execute('''
                                INSERT OR REPLACE INTO media_items
                                (imdb_id, tmdb_id, title, year, release_date, state, type, season_number, episode_number, episode_title, last_updated, metadata_updated, version, airtime, collected_at, genres, filled_by_file)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ''', (
                                imdb_id, tmdb_id, normalized_title, item.get('year'),
                                item.get('release_date'), 'Collected', 'episode',
                                item['season_number'], item['episode_number'], item.get('episode_title', ''),
                                datetime.now(), datetime.now(), 'unknown', airtime, collected_at, genres, filename
                            ))
                        logging.info(f"Added new item as Collected: {normalized_title} (ID: {cursor.lastrowid})")
                
                # Remove this line:
                # processed_filenames.add(filename)
                # (It's redundant now because we're already adding to all_valid_filenames earlier)

            except Exception as e:
                logging.error(f"Error processing item {item.get('title', 'Unknown')}: {str(e)}", exc_info=True)
                # Continue processing other items

        # Handle items not in the batch if not recent
        if not recent:
            for row in existing_items:
                item = row_to_dict(row)
                if item['filled_by_file'] and item['filled_by_file'] not in all_valid_filenames:
                    conn.execute('''
                        UPDATE media_items
                        SET state = ?, last_updated = ?, collected_at = NULL, filled_by_file = NULL
                        WHERE id = ?
                    ''', ('Wanted', datetime.now(), item['id']))
                    logging.info(f"Moved item to Wanted state (file no longer present): {item.get('title', 'Unknown')} (ID: {item['id']})")

        conn.commit()
        logging.debug(f"Collected items processed and database updated. Original batch: {len(media_items_batch)}, Filtered batch: {len(filtered_media_items_batch)}")
    except Exception as e:
        logging.error(f"Error adding collected items: {str(e)}", exc_info=True)
        conn.rollback()
        raise
    finally:
        conn.close()