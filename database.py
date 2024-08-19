import os
from sqlite3 import Row
import sqlite3
from typing import Dict, Any, List
import logging
from datetime import datetime, timedelta
import unicodedata
import json
from manual_blacklist import is_blacklisted
from upgrading_db import add_to_upgrading, remove_from_upgrading, create_upgrading_table
from settings import get_setting
from fuzzywuzzy import fuzz
from functools import wraps
import random
import time

def get_db_connection():
    db_path = os.path.join('db_content', 'media_items.db')
    conn = sqlite3.connect(db_path)
    conn.execute('PRAGMA journal_mode=WAL')  # Enable WAL mode
    conn.row_factory = sqlite3.Row
    return conn

def normalize_string(input_str):
    return ''.join(
        c for c in unicodedata.normalize('NFKD', input_str)
        if unicodedata.category(c) != 'Mn'
    )

def row_to_dict(row: Row) -> Dict[str, Any]:
    return {key: row[key] for key in row.keys()}

def retry_on_db_lock(max_attempts=5, initial_wait=0.1, backoff_factor=2):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            attempt = 0
            while attempt < max_attempts:
                try:
                    return func(*args, **kwargs)
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e) and attempt < max_attempts - 1:
                        attempt += 1
                        wait_time = initial_wait * (backoff_factor ** attempt) + random.uniform(0, 0.1)
                        logging.warning(f"Database locked. Retrying in {wait_time:.2f} seconds... (Attempt {attempt + 1}/{max_attempts})")
                        time.sleep(wait_time)
                    else:
                        raise
            raise Exception(f"Failed to execute {func.__name__} after {max_attempts} attempts due to database locks")
        return wrapper
    return decorator

def migrate_media_items_table():
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Step 1: Create a new table with the desired structure
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS media_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                imdb_id TEXT,
                tmdb_id TEXT,
                title TEXT,
                year INTEGER,
                release_date DATE,
                state TEXT,
                type TEXT,
                episode_title TEXT,
                season_number INTEGER,
                episode_number INTEGER,
                filled_by_title TEXT,
                filled_by_magnet TEXT,
                last_updated TIMESTAMP,
                metadata_updated TIMESTAMP,
                sleep_cycles INTEGER DEFAULT 0,
                last_checked TIMESTAMP,
                scrape_results TEXT,
                version TEXT,
                hybrid_flag TEXT,
                UNIQUE(imdb_id, tmdb_id, title, year, season_number, episode_number, version)
            )
        ''')

        # Step 2: Copy data from the old table to the new one
        cursor.execute('''
            INSERT INTO media_items_new 
            (imdb_id, tmdb_id, title, year, release_date, state, type, episode_title, 
             season_number, episode_number, filled_by_title, filled_by_magnet, 
             last_updated, metadata_updated, sleep_cycles, last_checked, scrape_results, version)
            SELECT 
                imdb_id, tmdb_id, title, year, release_date, state, type, episode_title, 
                season_number, episode_number, filled_by_title, filled_by_magnet, 
                last_updated, metadata_updated, sleep_cycles, last_checked, scrape_results, 
                COALESCE(version, 'default')
            FROM media_items
        ''')

        # Step 3: Drop the old table
        cursor.execute('DROP TABLE media_items')

        # Step 4: Rename the new table to the original name
        cursor.execute('ALTER TABLE media_items_new RENAME TO media_items')

        conn.commit()
        logging.info("Successfully migrated media_items table to include version in unique constraint.")
    except Exception as e:
        conn.rollback()
        logging.error(f"Error during media_items table migration: {str(e)}")
    finally:
        conn.close()

def create_tables():
    logging.info("Creating tables...")
    conn = get_db_connection()

    try:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS media_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                imdb_id TEXT,
                tmdb_id TEXT,
                title TEXT,
                year INTEGER,
                release_date DATE,
                state TEXT,
                type TEXT,
                episode_title TEXT,
                season_number INTEGER,
                episode_number INTEGER,
                filled_by_title TEXT,
                filled_by_magnet TEXT,
                last_updated TIMESTAMP,
                metadata_updated TIMESTAMP,
                sleep_cycles INTEGER DEFAULT 0,
                last_checked TIMESTAMP,
                scrape_results TEXT,
                version TEXT,
                UNIQUE(imdb_id, tmdb_id, title, year, season_number, episode_number, version)
            )
        ''')

        conn.commit()
    except Exception as e:
        logging.error(f"Error creating media_items table: {str(e)}")
    finally:
        conn.close()

def update_media_item_sleep_cycle(item_id, sleep_cycles):
    conn = get_db_connection()
    try:
        conn.execute('''
            UPDATE media_items
            SET sleep_cycles = ?, last_checked = ?
            WHERE id = ?
        ''', (sleep_cycles, datetime.now(), item_id))
        conn.commit()
        logging.debug(f"Updated sleep cycle for media item (ID: {item_id}) to {sleep_cycles}")
    except Exception as e:
        logging.error(f"Error updating sleep cycle for media item (ID: {item_id}): {str(e)}")
    finally:
        conn.close()


def get_metadata_updated(imdb_id=None, tmdb_id=None, title=None, year=None, season_number=None, episode_number=None):
    conn = get_db_connection()
    try:
        if season_number is not None and episode_number is not None:
            # Check for TV show episode
            query = '''
                SELECT metadata_updated FROM media_items
                WHERE imdb_id = ? AND season_number = ? AND episode_number = ?
            '''
            params = (imdb_id, season_number, episode_number)
        else:
            # Check for movie
            query = '''
                SELECT metadata_updated FROM media_items
                WHERE imdb_id = ?
            '''
            params = (imdb_id,)

        cursor = conn.execute(query, params)
        result = cursor.fetchone()
        return result['metadata_updated'] if result else None
    except Exception as e:
        logging.error(f"Error retrieving metadata_updated: {e}")
        return None
    finally:
        conn.close()

def update_metadata(item, metadata_date=None):
    conn = get_db_connection()
    try:
        if metadata_date is None:
            metadata_date = datetime.now()

        if 'season_number' in item and 'episode_number' in item:
            # It's an episode
            conn.execute('''
                UPDATE media_items
                SET tmdb_id = ?, title = ?, year = ?, release_date = ?, episode_title = ?, metadata_updated = ?
                WHERE imdb_id = ? AND season_number = ? AND episode_number = ?
            ''', (
                item['tmdb_id'], item['title'], item['year'], item.get('release_date', None),
                item.get('episode_title', ''), metadata_date,
                item['imdb_id'], item['season_number'], item['episode_number']
            ))
        else:
            # It's a movie
            conn.execute('''
                UPDATE media_items
                SET tmdb_id = ?, title = ?, year = ?, release_date = ?, metadata_updated = ?
                WHERE imdb_id = ?
            ''', (
                item['tmdb_id'], item['title'], item['year'], item.get('release_date', None),
                metadata_date, item['imdb_id']
            ))

        conn.commit()
        logging.debug(f"Updated metadata for {item['title']}")
    except Exception as e:
        logging.error(f"Error updating metadata: {str(e)}")
    finally:
        conn.close()

def is_metadata_stale(metadata_date_str):
    try:
        # Try parsing with fractional seconds
        metadata_date = datetime.strptime(metadata_date_str, '%Y-%m-%d %H:%M:%S.%f')
    except ValueError:
        # Fall back to parsing without fractional seconds
        metadata_date = datetime.strptime(metadata_date_str, '%Y-%m-%d %H:%M:%S')
    
    return (datetime.now() - metadata_date) > timedelta(days=7)

def add_wanted_items(media_items_batch: List[Dict[str, Any]], versions: Dict[str, bool]):
    conn = get_db_connection()
    try:
        items_added = 0
        items_updated = 0
        items_skipped = 0

        for item in media_items_batch:
            if not item.get('imdb_id'):
                logging.warning(f"Skipping item without valid IMDb ID: {item.get('title', 'Unknown')}")
                items_skipped += 1
                continue

            if is_blacklisted(item['imdb_id']):
                logging.debug(f"Skipping blacklisted item: {item.get('title', 'Unknown')} (IMDb ID: {item['imdb_id']})")
                items_skipped += 1
                continue

            normalized_title = normalize_string(item.get('title', 'Unknown'))
            item_type = 'episode' if 'season_number' in item and 'episode_number' in item else 'movie'

            # Check if any version of the item is already collected
            if item_type == 'movie':
                any_version_collected = conn.execute('''
                    SELECT id FROM media_items
                    WHERE imdb_id = ? AND type = 'movie' AND state = 'Collected'
                ''', (item['imdb_id'],)).fetchone()
            else:
                any_version_collected = conn.execute('''
                    SELECT id FROM media_items
                    WHERE imdb_id = ? AND type = 'episode' AND season_number = ? AND episode_number = ? AND state = 'Collected'
                ''', (item['imdb_id'], item['season_number'], item['episode_number'])).fetchone()

            if any_version_collected:
                logging.debug(f"Skipping item as it's already collected in some version: {normalized_title}")
                items_skipped += 1
                continue

            for version, enabled in versions.items():
                if not enabled:
                    continue

                # Check if item exists for this version
                if item_type == 'movie':
                    existing_item = conn.execute('''
                        SELECT id, release_date, state FROM media_items
                        WHERE imdb_id = ? AND type = 'movie' AND version = ?
                    ''', (item['imdb_id'], version)).fetchone()
                else:
                    existing_item = conn.execute('''
                        SELECT id, release_date, state FROM media_items
                        WHERE imdb_id = ? AND type = 'episode' AND season_number = ? AND episode_number = ? AND version = ?
                    ''', (item['imdb_id'], item['season_number'], item['episode_number'], version)).fetchone()

                if existing_item:
                    # Item exists, check if we need to update
                    if existing_item['state'] == 'Blacklisted':
                        logging.debug(f"Skipping update for blacklisted item: {normalized_title} (Version: {version})")
                        items_skipped += 1
                    elif existing_item['state'] != 'Collected':
                        if existing_item['release_date'] != item.get('release_date'):
                            conn.execute('''
                                UPDATE media_items
                                SET release_date = ?, last_updated = ?, state = ?
                                WHERE id = ?
                            ''', (item.get('release_date'), datetime.now(), 'Wanted', existing_item['id']))
                            logging.debug(f"Updated release date for existing item: {normalized_title} (Version: {version})")
                            items_updated += 1
                        else:
                            logging.debug(f"Skipping update for existing item: {normalized_title} (Version: {version})")
                            items_skipped += 1
                    else:
                        logging.debug(f"Skipping update for collected item: {normalized_title} (Version: {version})")
                        items_skipped += 1
                else:
                    # Insert new item
                    if item_type == 'movie':
                        conn.execute('''
                            INSERT INTO media_items
                            (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated, version)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            item['imdb_id'], item.get('tmdb_id'), normalized_title, item.get('year'),
                            item.get('release_date'), 'Wanted', 'movie', datetime.now(), version
                        ))
                    else:
                        conn.execute('''
                            INSERT INTO media_items
                            (imdb_id, tmdb_id, title, year, release_date, state, type, season_number, episode_number, episode_title, last_updated, version)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            item['imdb_id'], item.get('tmdb_id'), normalized_title, item.get('year'),
                            item.get('release_date'), 'Wanted', 'episode',
                            item['season_number'], item['episode_number'], item.get('episode_title', ''),
                            datetime.now(), version
                        ))
                    logging.debug(f"Adding new {'movie' if item_type == 'movie' else 'episode'} as Wanted in DB: {normalized_title} (Version: {version})")
                    items_added += 1

        conn.commit()
        logging.debug(f"Wanted items processing complete. Added: {items_added}, Updated: {items_updated}, Skipped: {items_skipped}")
    except Exception as e:
        logging.error(f"Error adding wanted items: {str(e)}")
        conn.rollback()
    finally:
        conn.close()

def get_versions_for_item(item_id):
    conn = get_db_connection()
    try:
        cursor = conn.execute('''
            SELECT version FROM media_items
            WHERE id = ?
        ''', (item_id,))
        versions = [row['version'] for row in cursor.fetchall()]
        return versions
    except Exception as e:
        logging.error(f"Error retrieving versions for item ID {item_id}: {str(e)}")
        return []
    finally:
        conn.close()

def update_item_versions(item_id, versions):
    conn = get_db_connection()
    try:
        # First, remove all existing versions for this item
        conn.execute('''
            DELETE FROM media_items
            WHERE id = ?
        ''', (item_id,))

        # Then, insert new records for each version
        item_data = get_media_item_by_id(item_id)
        if item_data:
            for version in versions:
                if item_data['type'] == 'movie':
                    conn.execute('''
                        INSERT INTO media_items
                        (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated, version)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        item_data['imdb_id'], item_data['tmdb_id'], item_data['title'], item_data['year'],
                        item_data['release_date'], item_data['state'], 'movie', datetime.now(), version
                    ))
                else:
                    conn.execute('''
                        INSERT INTO media_items
                        (imdb_id, tmdb_id, title, year, release_date, state, type, season_number, episode_number, episode_title, last_updated, version)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        item_data['imdb_id'], item_data['tmdb_id'], item_data['title'], item_data['year'],
                        item_data['release_date'], item_data['state'], 'episode',
                        item_data['season_number'], item_data['episode_number'], item_data['episode_title'],
                        datetime.now(), version
                    ))
        
        conn.commit()
        logging.debug(f"Updated versions for item ID {item_id}: {versions}")
    except Exception as e:
        logging.error(f"Error updating versions for item ID {item_id}: {str(e)}")
        conn.rollback()
    finally:
        conn.close()

def get_all_items_for_content_source(source_type):
    conn = get_db_connection()
    try:
        cursor = conn.execute('''
            SELECT * FROM media_items
            WHERE source = ?
        ''', (source_type,))
        items = [dict(row) for row in cursor.fetchall()]
        return items
    except Exception as e:
        logging.error(f"Error retrieving items for content source {source_type}: {str(e)}")
        return []
    finally:
        conn.close()

def update_content_source_for_item(item_id, source_type):
    conn = get_db_connection()
    try:
        conn.execute('''
            UPDATE media_items
            SET source = ?, last_updated = ?
            WHERE id = ?
        ''', (source_type, datetime.now(), item_id))
        conn.commit()
        logging.debug(f"Updated content source for item ID {item_id} to {source_type}")
    except Exception as e:
        logging.error(f"Error updating content source for item ID {item_id}: {str(e)}")
        conn.rollback()
    finally:
        conn.close()

def add_collected_items(media_items_batch, recent=False):
    conn = get_db_connection()
    try:
        conn.execute('BEGIN TRANSACTION')
        
        processed_items = set()

        existing_items = conn.execute('SELECT id, imdb_id, type, season_number, episode_number, state, version, filled_by_file FROM media_items').fetchall()
        existing_ids = {}
        for row in map(row_to_dict, existing_items):
            if row['type'] == 'movie':
                key = (row['imdb_id'], 'movie', row['version'])
            else:
                key = (row['imdb_id'], 'episode', row['season_number'], row['episode_number'], row['version'])
            existing_ids[key] = (row['id'], row['state'], row['filled_by_file'])

        scraping_versions = get_setting('Scraping', 'versions', {})
        versions = list(scraping_versions.keys())

        for item in media_items_batch:
            if not item.get('imdb_id'):
                logging.warning(f"Skipping item without valid IMDb ID: {item.get('title', 'Unknown')}")
                continue

            normalized_title = normalize_string(item.get('title', 'Unknown'))
            item_type = 'episode' if 'season_number' in item and 'episode_number' in item else 'movie'

            plex_filename = os.path.basename(item.get('location', ''))

            item_found = False
            for version in versions:
                if item_type == 'movie':
                    item_key = (item['imdb_id'], 'movie', version)
                else:
                    item_key = (item['imdb_id'], 'episode', item['season_number'], item['episode_number'], version)

                if item_key in existing_ids:
                    item_found = True
                    item_id, current_state, filled_by_file = existing_ids[item_key]

                    logging.debug(f"Comparing existing item in DB with Plex item:")
                    logging.debug(f"  DB Item: {normalized_title} (ID: {item_id}, State: {current_state}, Version: {version})")
                    logging.debug(f"  DB Filled By File: {filled_by_file}")
                    logging.debug(f"  Plex Filename: {plex_filename}")

                    if filled_by_file and plex_filename:
                        match_ratio = fuzz.ratio(filled_by_file.lower(), plex_filename.lower())
                        logging.debug(f"  Fuzzy match ratio: {match_ratio}%")

                        if match_ratio >= 95:  # You can adjust this threshold
                            logging.debug(f"  Match found: DB Filled By File matches Plex Filename (Fuzzy match: {match_ratio}%)")

                            if current_state != 'Collected':
                                conn.execute('''
                                    UPDATE media_items
                                    SET state = ?, last_updated = ?
                                    WHERE id = ?
                                ''', ('Collected', datetime.now(), item_id))
                                logging.debug(f"Updating item in DB to Collected: {normalized_title} (Version: {version})")
                        else:
                            logging.debug(f"  No match: DB Filled By File does not match Plex Filename (Fuzzy match: {match_ratio}%)")
                    elif current_state == 'Collected':
                        logging.debug(f"Item already Collected, keeping state: {normalized_title} (Version: {version})")
                    else:
                        logging.debug(f"Cannot compare item, keeping current state: {normalized_title} (Version: {version})")

                    processed_items.add(item_id)

            if not item_found:
                # Check if the item already exists with 'unknown' version
                if item_type == 'movie':
                    cursor = conn.execute('''
                        SELECT id FROM media_items
                        WHERE imdb_id = ? AND type = 'movie' AND version = 'unknown'
                    ''', (item['imdb_id'],))
                else:
                    cursor = conn.execute('''
                        SELECT id FROM media_items
                        WHERE imdb_id = ? AND type = 'episode' AND season_number = ? AND episode_number = ? AND version = 'unknown'
                    ''', (item['imdb_id'], item['season_number'], item['episode_number']))

                existing_item = cursor.fetchone()

                if existing_item:
                    # Update existing item
                    item_id = existing_item[0]
                    conn.execute('''
                        UPDATE media_items
                        SET state = ?, last_updated = ?
                        WHERE id = ?
                    ''', ('Collected', datetime.now(), item_id))
                    logging.debug(f"Updating existing item to Collected: {normalized_title}")
                else:
                    # Insert new item
                    if item_type == 'movie':
                        cursor = conn.execute('''
                            INSERT INTO media_items
                            (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated, metadata_updated, version)
                            VALUES (?,?,?,?,?,?,?,?,?,?)
                        ''', (
                            item['imdb_id'], item.get('tmdb_id'), normalized_title, item.get('year'),
                            item.get('release_date'), 'Collected', 'movie',
                            datetime.now(), datetime.now(), 'unknown'
                        ))
                        logging.info(f"Adding new movie to DB as Collected: {normalized_title}")
                    else:
                        cursor = conn.execute('''
                            INSERT INTO media_items
                            (imdb_id, tmdb_id, title, year, release_date, state, type, season_number, episode_number, episode_title, last_updated, metadata_updated, version)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ''', (
                            item['imdb_id'], item.get('tmdb_id'), normalized_title, item.get('year'),
                            item.get('release_date'), 'Collected', 'episode',
                            item['season_number'], item['episode_number'], item.get('episode_title', ''),
                            datetime.now(), datetime.now(), 'unknown'
                        ))
                        logging.info(f"Adding new episode to DB as Collected: {normalized_title} S{item['season_number']}E{item['episode_number']}")
                
                new_item_id = cursor.lastrowid
                processed_items.add(new_item_id)

        # Handle items not in the batch if not recent
        if not recent:
            items_to_check = set(id for id, _, _ in existing_ids.values()) - processed_items
            for item_id in items_to_check:
                item_info = conn.execute('SELECT title, state, version FROM media_items WHERE id = ?', (item_id,)).fetchone()
                if item_info:
                    title, state, version = item_info
                    if state not in ['Collected', 'Blacklisted']:
                        conn.execute('UPDATE media_items SET state = ?, last_updated = ? WHERE id = ?', ('Wanted', datetime.now(), item_id))
                        logging.debug(f"Moving non-Collected/non-Blacklisted item back to Wanted state: ID {item_id}, {title} (Version: {version})")
                    else:
                        logging.debug(f"Keeping {state} item in DB: ID {item_id}, {title} (Version: {version})")

        conn.commit()
        logging.debug(f"Collected items processed and database updated. Total items: {len(media_items_batch)}, Processed: {len(processed_items)}")
    except Exception as e:
        logging.error(f"Error adding collected items: {str(e)}", exc_info=True)
        conn.rollback()
        raise
    finally:
        conn.close()

        
@retry_on_db_lock()
def update_media_item_state(item_id, state, **kwargs):
    conn = get_db_connection()
    try:
        conn.execute('BEGIN TRANSACTION')
        
        # Prepare the base query
        query = '''
            UPDATE media_items
            SET state = ?, last_updated = ?
        '''
        params = [state, datetime.now()]

        # Add optional fields to the query if they are provided
        optional_fields = ['filled_by_title', 'filled_by_magnet', 'filled_by_file', 'scrape_results', 'hybrid_flag']
        for field in optional_fields:
            if field in kwargs:
                query += f", {field} = ?"
                value = kwargs[field]
                if field == 'scrape_results':
                    value = json.dumps(value) if value else None
                params.append(value)

        # Complete the query
        query += " WHERE id = ?"
        params.append(item_id)

        # Execute the query
        conn.execute(query, params)

        if state == 'Scraping':
            item = conn.execute('SELECT * FROM media_items WHERE id = ?', (item_id,)).fetchone()
            if item:
                add_to_upgrading(dict(item))

        conn.commit()

        logging.debug(f"Updated media item (ID: {item_id}) state to {state}")
        for field in optional_fields:
            if field in kwargs:
                logging.debug(f"  {field}: {kwargs[field]}")

    except Exception as e:
        logging.error(f"Error updating media item (ID: {item_id}): {str(e)}")
        conn.rollback()
        raise
    finally:
        conn.close()
        
def get_media_item_by_id(item_id):
    conn = get_db_connection()
    try:
        cursor = conn.execute('SELECT * FROM media_items WHERE id = ?', (item_id,))
        item = cursor.fetchone()
        if item:
            item_dict = dict(item)
            item_dict['hybrid_flag'] = item_dict.get('hybrid_flag', None)  # Ensure hybrid_flag is included
            return item_dict
        return None
    except Exception as e:
        logging.error(f"Error retrieving media item (ID: {item_id}): {str(e)}")
        return None
    finally:
        conn.close()

def get_media_item_status(imdb_id=None, tmdb_id=None, title=None, year=None, season_number=None, episode_number=None):
    conn = get_db_connection()
    try:
        if season_number is not None and episode_number is not None:
            # Check for TV show episode
            query = '''
                SELECT state FROM media_items
                WHERE imdb_id = ? AND season_number = ? AND episode_number = ?
            '''
            params = (imdb_id, season_number, episode_number)
        else:
            # Check for movie
            query = '''
                SELECT state FROM media_items
                WHERE imdb_id = ?
            '''
            params = (imdb_id,)

        cursor = conn.execute(query, params)
        result = cursor.fetchone()
        conn.close()

        return result['state'] if result else "Missing"
    except Exception as e:
        logging.error(f"Error retrieving media item status: {e}")
        return "Missing"
    finally:
        conn.close()
        
def get_media_item_presence(imdb_id=None, tmdb_id=None):
    conn = get_db_connection()
    try:
        # Determine the query and parameters based on provided IDs
        if imdb_id is not None:
            id_field = 'imdb_id'
            id_value = imdb_id
        elif tmdb_id is not None:
            id_field = 'tmdb_id'
            id_value = tmdb_id
        else:
            raise ValueError("Either imdb_id or tmdb_id must be provided.")

        # Check for a matching item in the database
        query = f'''
            SELECT state FROM media_items
            WHERE {id_field} = ?
        '''
        params = (id_value,)

        cursor = conn.execute(query, params)
        result = cursor.fetchone()

        return result['state'] if result else "Missing"
    except ValueError as ve:
        logging.error(f"Invalid input: {ve}")
        return "Missing"
    except Exception as e:
        logging.error(f"Error retrieving media item status: {e}")
        return "Missing"
    finally:
        conn.close()

        
# Modify the create_database function to include creating the upgrading table
def create_database():
    create_tables()
    create_upgrading_table()
    logging.info("Database created and tables initialized.")

# Modify the verify_database function to include verifying the upgrading table
def verify_database():
    logging.info("Starting database verification...")
    create_tables()
    create_upgrading_table()
    
    # Verify that the tables were actually created
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='media_items'")
    if cursor.fetchone():
        logging.info("media_items table exists.")
    else:
        logging.error("media_items table does not exist!")
    conn.close()
    
    add_hybrid_flag_column()
    add_filled_by_file_column()

    logging.info("Database verification complete.")

def get_all_media_items(state=None, media_type=None, tmdb_id=None):
    conn = get_db_connection()
    query = 'SELECT * FROM media_items WHERE 1=1'
    params = []
    if state:
        query += ' AND state = ?'
        params.append(state)
    if media_type:
        query += ' AND type = ?'
        params.append(media_type)
    if tmdb_id:
        query += ' AND tmdb_id = ?'
        params.append(tmdb_id)
    cursor = conn.execute(query, params)
    items = cursor.fetchall()
    conn.close()
    return items

def search_movies(search_term):
    conn = get_db_connection()
    cursor = conn.execute('SELECT * FROM media_items WHERE type = "movie" AND title LIKE ?', (f'%{search_term}%',))
    items = cursor.fetchall()
    conn.close()
    return items

def search_tv_shows(search_term):
    conn = get_db_connection()
    cursor = conn.execute('SELECT * FROM media_items WHERE type = "episode" AND (title LIKE ? OR episode_title LIKE ?)', (f'%{search_term}%', f'%{search_term}%'))
    items = cursor.fetchall()
    conn.close()
    return items

def purge_database(content_type=None, state=None):
    conn = get_db_connection()
    try:
        query = 'DELETE FROM media_items WHERE 1=1'
        params = []

        if content_type != 'all':
            query += ' AND type = ?'
            params.append(content_type)

        if state == 'working':
            query += ' AND state NOT IN (?, ?, ?)'
            params.extend(['Wanted', 'Collected', 'Blacklisted'])
        elif state != 'all':
            query += ' AND state = ?'
            params.append(state)

        logging.debug(f"Executing query: {query} with params: {params}")
        conn.execute(query, params)
        conn.commit()
        logging.info(f"Database purged successfully for type '{content_type}' and state '{state}'.")
    except Exception as e:
        logging.error(f"Error purging database: {e}")
    finally:
        conn.close()
    create_tables()

def remove_from_media_items(item_id):
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM media_items WHERE id = ?', (item_id,))
        conn.commit()
        logging.info(f"Removed item (ID: {item_id}) from media items")
    except Exception as e:
        logging.error(f"Error removing item (ID: {item_id}) from media items: {str(e)}")
    finally:
        conn.close()

def get_title_by_imdb_id(imdb_id: str) -> str:
    logging.debug(f"Looking up title for IMDb ID: {imdb_id}")
    conn = get_db_connection()
    cursor = conn.execute('''
        SELECT title FROM media_items WHERE imdb_id = ?
        UNION
        SELECT title FROM media_items WHERE tmdb_id = ?
    ''', (imdb_id, imdb_id))
    result = cursor.fetchone()
    conn.close()
    return result['title'] if result else None

def get_item_state(item_id: int) -> str:
    conn = get_db_connection()
    try:
        cursor = conn.execute('SELECT state FROM media_items WHERE id = ?', (item_id,))
        result = cursor.fetchone()
        if result:
            return result['state']
        else:
            logging.warning(f"No state found for item ID: {item_id}")
            return None
    except Exception as e:
        logging.error(f"Error getting state for item ID: {item_id}: {str(e)}")
        return None
    finally:
        conn.close()

def get_blacklisted_items():
    conn = get_db_connection()
    try:
        cursor = conn.execute('SELECT * FROM media_items WHERE state = "Blacklisted"')
        items = cursor.fetchall()
        return [dict(item) for item in items]
    except Exception as e:
        logging.error(f"Error retrieving blacklisted items: {str(e)}")
        return []
    finally:
        conn.close()

def remove_from_blacklist(item_ids: List[int]):
    conn = get_db_connection()
    try:
        for item_id in item_ids:
            conn.execute('''
                UPDATE media_items
                SET state = 'Wanted', last_updated = ?, sleep_cycles = 0
                WHERE id = ? AND state = 'Blacklisted'
            ''', (datetime.now(), item_id))
        conn.commit()
        logging.info(f"Removed {len(item_ids)} items from blacklist")
    except Exception as e:
        logging.error(f"Error removing items from blacklist: {str(e)}")
        conn.rollback()
    finally:
        conn.close()
        
def update_release_date_and_state(item_id, release_date, new_state):
    conn = get_db_connection()
    try:
        # First, fetch the current item data
        cursor = conn.execute('SELECT * FROM media_items WHERE id = ?', (item_id,))
        item = cursor.fetchone()
        
        if item:
            conn.execute('''
                UPDATE media_items
                SET release_date = ?, state = ?, last_updated = ?
                WHERE id = ?
            ''', (release_date, new_state, datetime.now(), item_id))
            conn.commit()
            
            # Create item description based on the type of media
            if item['type'] == 'episode':
                item_description = f"{item['title']} S{item['season_number']:02d}E{item['episode_number']:02d}"
            else:  # movie
                item_description = f"{item['title']} ({item['year']})"
            
            logging.debug(f"Updated release date to {release_date} and state to {new_state} for {item_description}")
        else:
            logging.error(f"No item found with ID {item_id}")
    except Exception as e:
        logging.error(f"Error updating release date and state for item ID {item_id}: {str(e)}")
    finally:
        conn.close()

def update_year(item_id: int, year: int):
    conn = get_db_connection()
    try:
        conn.execute('''
            UPDATE media_items
            SET year = ?, last_updated = ?
            WHERE id = ?
        ''', (year, datetime.now(), item_id))
        conn.commit()
        logging.info(f"Updated year to {year} for item ID {item_id}")
    except Exception as e:
        logging.error(f"Error updating year for item ID {item_id}: {str(e)}")
    finally:
        conn.close()

def add_hybrid_flag_column():
    conn = get_db_connection()
    try:
        conn.execute('ALTER TABLE media_items ADD COLUMN hybrid_flag TEXT')
        conn.commit()
        logging.info("Successfully added hybrid_flag column to media_items table.")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e):
            logging.info("hybrid_flag column already exists in media_items table.")
        else:
            logging.error(f"Error adding hybrid_flag column: {str(e)}")
    except Exception as e:
        logging.error(f"Unexpected error adding hybrid_flag column: {str(e)}")
    finally:
        conn.close()

def add_filled_by_file_column():
    conn = get_db_connection()
    try:
        conn.execute('ALTER TABLE media_items ADD COLUMN filled_by_file TEXT')
        conn.commit()
        logging.info("Successfully added filled_by_file column to media_items table.")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e):
            logging.info("filled_by_file column already exists in media_items table.")
        else:
            logging.error(f"Error adding filled_by_file column: {str(e)}")
    finally:
        conn.close()
