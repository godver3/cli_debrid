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
import aiohttp
import asyncio
from poster_cache import get_cached_poster_url, cache_poster_url, clean_expired_cache
from aiohttp import ClientConnectorError, ClientResponseError, ServerTimeoutError

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
            CREATE TABLE IF NOT EXISTS media_items_new (
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
                collected_at TIMESTAMP,
                genres TEXT,
                UNIQUE(imdb_id, tmdb_id, title, year, season_number, episode_number, version)
            )
        ''')

        # Step 2: Copy data from the old table to the new one
        cursor.execute('''
            INSERT INTO media_items_new 
            (imdb_id, tmdb_id, title, year, release_date, state, type, episode_title, 
             season_number, episode_number, filled_by_title, filled_by_magnet, 
             last_updated, metadata_updated, sleep_cycles, last_checked, scrape_results, version,
             hybrid_flag, collected_at, genres)
            SELECT 
                imdb_id, tmdb_id, title, year, release_date, state, type, episode_title, 
                season_number, episode_number, filled_by_title, filled_by_magnet, 
                last_updated, metadata_updated, sleep_cycles, last_checked, scrape_results, 
                COALESCE(version, 'default'),
                hybrid_flag,
                CASE WHEN state = 'Collected' THEN last_updated ELSE NULL END,
                genres
            FROM media_items
        ''')

        # Step 3: Drop the old table
        cursor.execute('DROP TABLE media_items')

        # Step 4: Rename the new table to the original name
        cursor.execute('ALTER TABLE media_items_new RENAME TO media_items')

        conn.commit()
        logging.info("Successfully migrated media_items table to include collected_at column.")
    except Exception as e:
        conn.rollback()
        logging.error(f"Error during media_items table migration: {str(e)}")
    finally:
        conn.close()

def create_tables():
    #logging.info("Creating tables...")
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
                genres TEXT,
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

def get_existing_airtime(conn, imdb_id):
    cursor = conn.execute('''
        SELECT airtime FROM media_items
        WHERE imdb_id = ? AND type = 'episode' AND airtime IS NOT NULL
        LIMIT 1
    ''', (imdb_id,))
    result = cursor.fetchone()
    return result[0] if result else None

def add_wanted_items(media_items_batch: List[Dict[str, Any]], versions: Dict[str, bool]):
    from metadata.metadata import get_show_airtime_by_imdb_id

    logging.debug(f"add_wanted_items called with versions type: {type(versions)}")
    logging.debug(f"versions content: {versions}")

    conn = get_db_connection()
    try:
        items_added = 0
        items_updated = 0
        items_skipped = 0
        airtime_cache = {}  # Cache to store airtimes for each show

        # Handle different types of versions input
        if isinstance(versions, str):
            try:
                versions = json.loads(versions)
            except json.JSONDecodeError:
                logging.error(f"Invalid JSON string for versions: {versions}")
                versions = {}
        elif isinstance(versions, list):
            versions = {version: True for version in versions}
        
        if not isinstance(versions, dict):
            logging.error(f"Unexpected type for versions: {type(versions)}. Using empty dict.")
            versions = {}

        logging.debug(f"Processed versions: {versions}")

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

            genres = json.dumps(item.get('genres', []))  # Convert genres list to JSON string

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
                            (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated, version, genres)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            item['imdb_id'], item.get('tmdb_id'), normalized_title, item.get('year'),
                            item.get('release_date'), 'Wanted', 'movie', datetime.now(), version, genres
                        ))
                    else:
                        # For episodes, get the airtime
                        if item['imdb_id'] not in airtime_cache:
                            airtime_cache[item['imdb_id']] = get_existing_airtime(conn, item['imdb_id'])
                            if airtime_cache[item['imdb_id']] is None:
                                logging.debug(f"No existing airtime found for show {item['imdb_id']}, fetching from metadata")
                                airtime_cache[item['imdb_id']] = get_show_airtime_by_imdb_id(item['imdb_id'])
                            
                            # Ensure we always have a default airtime
                            if not airtime_cache[item['imdb_id']]:
                                airtime_cache[item['imdb_id']] = '19:00'
                                logging.debug(f"No airtime found, defaulting to 19:00 for show {item['imdb_id']}")
                            
                            logging.debug(f"Airtime for show {item['imdb_id']} set to {airtime_cache[item['imdb_id']]}")
                        
                        airtime = airtime_cache[item['imdb_id']]
                        
                        conn.execute('''
                            INSERT INTO media_items
                            (imdb_id, tmdb_id, title, year, release_date, state, type, season_number, episode_number, episode_title, last_updated, version, airtime, genres)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            item['imdb_id'], item.get('tmdb_id'), normalized_title, item.get('year'),
                            item.get('release_date'), 'Wanted', 'episode',
                            item['season_number'], item['episode_number'], item.get('episode_title', ''),
                            datetime.now(), version, airtime, genres
                        ))
                    logging.debug(f"Adding new {'movie' if item_type == 'movie' else 'episode'} as Wanted in DB: {normalized_title} (Version: {version}, Airtime: {airtime if item_type == 'episode' else 'N/A'})")
                    items_added += 1

        conn.commit()
        logging.debug(f"Wanted items processing complete. Added: {items_added}, Updated: {items_updated}, Skipped: {items_skipped}")
        logging.debug(f"Airtime cache contents: {airtime_cache}")
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
    from metadata.metadata import get_show_airtime_by_imdb_id

    conn = get_db_connection()
    try:
        conn.execute('BEGIN TRANSACTION')
        
        processed_items = set()
        airtime_cache = {}  # Cache to store airtimes for each show

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

            genres = json.dumps(item.get('genres', []))  # Convert genres list to JSON string

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
                                    SET state = ?, last_updated = ?, collected_at = ?
                                    WHERE id = ?
                                ''', ('Collected', datetime.now(), datetime.now(), item_id))
                                logging.debug(f"Updating item in DB to Collected: {normalized_title} (Version: {version})")
                        else:
                            logging.debug(f"  No match: DB Filled By File does not match Plex Filename (Fuzzy match: {match_ratio}%)")
                    elif current_state == 'Collected':
                        logging.debug(f"Item already Collected, keeping state: {normalized_title} (Version: {version})")
                    else:
                        logging.debug(f"Cannot compare item, keeping current state: {normalized_title} (Version: {version})")

                    processed_items.add(item_id)

            if not item_found:
                if item_type == 'movie':
                    cursor = conn.execute('''
                        SELECT id FROM media_items
                        WHERE imdb_id = ? AND type = 'movie' AND version = 'unknown'
                    ''', (item['imdb_id'],))
                else:
                    # For episodes, get the airtime
                    if item['imdb_id'] not in airtime_cache:
                        airtime_cache[item['imdb_id']] = get_existing_airtime(conn, item['imdb_id'])
                        if airtime_cache[item['imdb_id']] is None:
                            logging.debug(f"No existing airtime found for show {item['imdb_id']}, fetching from metadata")
                            airtime_cache[item['imdb_id']] = get_show_airtime_by_imdb_id(item['imdb_id'])
                        
                        # Ensure we always have a default airtime
                        if not airtime_cache[item['imdb_id']]:
                            airtime_cache[item['imdb_id']] = '19:00'
                            logging.debug(f"No airtime found, defaulting to 19:00 for show {item['imdb_id']}")
                        
                        logging.debug(f"Airtime for show {item['imdb_id']} set to {airtime_cache[item['imdb_id']]}")
                    
                    airtime = airtime_cache[item['imdb_id']]

                    cursor = conn.execute('''
                        SELECT id FROM media_items
                        WHERE imdb_id = ? AND type = 'episode' AND season_number = ? AND episode_number = ? AND version = 'unknown'
                    ''', (item['imdb_id'], item['season_number'], item['episode_number']))

                existing_item = cursor.fetchone()

                if existing_item:
                    # Update existing item
                    item_id = existing_item[0]
                    if item_type == 'movie':
                        conn.execute('''
                            UPDATE media_items
                            SET state = ?, last_updated = ?
                            WHERE id = ?
                        ''', ('Collected', datetime.now(), item_id))
                    else:
                        conn.execute('''
                            UPDATE media_items
                            SET state = ?, last_updated = ?, airtime = ?
                            WHERE id = ?
                        ''', ('Collected', datetime.now(), airtime, item_id))
                    logging.debug(f"Updating existing item to Collected: {normalized_title}")
                else:
                    # Insert new item
                    if item_type == 'movie':
                        cursor = conn.execute('''
                            INSERT INTO media_items
                            (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated, metadata_updated, version, collected_at, genres)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                        ''', (
                            item['imdb_id'], item.get('tmdb_id'), normalized_title, item.get('year'),
                            item.get('release_date'), 'Collected', 'movie',
                            datetime.now(), datetime.now(), 'unknown', datetime.now(), genres
                        ))
                        logging.info(f"Adding new movie to DB as Collected: {normalized_title}")
                    else:
                        cursor = conn.execute('''
                            INSERT INTO media_items
                            (imdb_id, tmdb_id, title, year, release_date, state, type, season_number, episode_number, episode_title, last_updated, metadata_updated, version, airtime, collected_at, genres)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ''', (
                            item['imdb_id'], item.get('tmdb_id'), normalized_title, item.get('year'),
                            item.get('release_date'), 'Collected', 'episode',
                            item['season_number'], item['episode_number'], item.get('episode_title', ''),
                            datetime.now(), datetime.now(), 'unknown', airtime, datetime.now(), genres
                        ))
                        logging.info(f"Adding new episode to DB as Collected: {normalized_title} S{item['season_number']}E{item['episode_number']} (Airtime: {airtime})")
                
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
                        conn.execute('UPDATE media_items SET state = ?, last_updated = ?, collected_at = NULL WHERE id = ?', ('Wanted', datetime.now(), item_id))
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

        # Update collected_at if the state is changing to 'Collected'
        if state == 'Collected':
            query += ", collected_at = ?"
            params.append(datetime.now())

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
    #logging.info("Database created and tables initialized.")

def add_collected_at_column():
    conn = get_db_connection()
    try:
        conn.execute('ALTER TABLE media_items ADD COLUMN collected_at TIMESTAMP')
        conn.commit()
        logging.info("Successfully added collected_at column to media_items table.")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            logging.error(f"Error adding collected_at column: {str(e)}")
    finally:
        conn.close()

def verify_database():
    #logging.info("Starting database verification...")
    create_tables()
    create_upgrading_table()
    
    # Verify that the tables were actually created
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='media_items'")
    if not cursor.fetchone():
        logging.error("media_items table does not exist!")
    conn.close()
    
    add_hybrid_flag_column()
    add_filled_by_file_column()
    add_airtime_column()
    add_collected_at_column()  # Add this line

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

        trakt_cache_file = 'db_content/trakt_last_activity.pkl'
        if os.path.exists(trakt_cache_file):
            os.remove(trakt_cache_file)
            logging.info(f"Deleted Trakt cache file: {trakt_cache_file}")
        else:
            logging.info(f"Trakt cache file not found: {trakt_cache_file}")

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
        if "duplicate column name" not in str(e):
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
        if "duplicate column name" not in str(e):
            logging.info("filled_by_file column already exists in media_items table.")
    finally:
        conn.close()

def add_airtime_column():
    conn = get_db_connection()
    try:
        conn.execute('ALTER TABLE media_items ADD COLUMN airtime TEXT')
        conn.commit()
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            logging.error(f"Error adding airtime column: {str(e)}")
    finally:
        conn.close()

def get_collected_counts():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # Count unique collected movies
        cursor.execute('''
            SELECT COUNT(DISTINCT imdb_id) 
            FROM media_items 
            WHERE type = 'movie' AND state = 'Collected'
        ''')
        total_movies = cursor.fetchone()[0]

        # Count unique collected TV shows
        cursor.execute('''
            SELECT COUNT(DISTINCT imdb_id) 
            FROM media_items 
            WHERE type = 'episode' AND state = 'Collected'
        ''')
        total_shows = cursor.fetchone()[0]

        # Count total collected episodes
        cursor.execute('''
            SELECT COUNT(*) 
            FROM media_items 
            WHERE type = 'episode' AND state = 'Collected'
        ''')
        total_episodes = cursor.fetchone()[0]

        return {
            'total_movies': total_movies,
            'total_shows': total_shows,
            'total_episodes': total_episodes
        }
    except Exception as e:
        logging.error(f"Error getting collected counts: {str(e)}")
        return {'total_movies': 0, 'total_shows': 0, 'total_episodes': 0}
    finally:
        conn.close()

def bulk_delete_by_imdb_id(imdb_id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM media_items WHERE imdb_id = ?', (imdb_id,))
        deleted_count = cursor.rowcount
        conn.commit()
        return deleted_count
    except Exception as e:
        logging.error(f"Error bulk deleting items with IMDB ID {imdb_id}: {str(e)}")
        return 0
    finally:
        conn.close()

from poster_cache import get_cached_poster_url, cache_poster_url, clean_expired_cache

async def get_recently_added_items(movie_limit=50, show_limit=50):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # Query for movies
        movie_query = """
        SELECT title, year, type, collected_at, imdb_id, tmdb_id, version
        FROM media_items
        WHERE type = 'movie' AND collected_at IS NOT NULL
        ORDER BY collected_at DESC
        LIMIT ?
        """
        
        # Query for episodes
        episode_query = """
        SELECT title, year, type, season_number, episode_number, collected_at, imdb_id, tmdb_id, version
        FROM media_items
        WHERE type = 'episode' AND collected_at IS NOT NULL
        ORDER BY collected_at DESC
        """
        
        cursor.execute(movie_query, (movie_limit,))
        movie_results = cursor.fetchall()
        
        cursor.execute(episode_query)
        episode_results = cursor.fetchall()
        
        logging.debug(f"Initial movie results: {len(movie_results)}")
        for movie in movie_results:
            logging.debug(f"Movie: {movie['title']} ({movie['year']}) - Version: {movie['version']}")
        
        consolidated_movies = {}
        shows = {}
        
        async with aiohttp.ClientSession() as session:
            poster_tasks = []
            
            # Process movies
            for row in movie_results:
                item = dict(row)
                key = f"{item['title']}-{item['year']}"
                if key not in consolidated_movies:
                    consolidated_movies[key] = {
                        **item,
                        'versions': [item['version']],
                        'collected_at': item['collected_at']
                    }
                    media_type = 'movie'
                    cached_url = get_cached_poster_url(item['tmdb_id'], media_type)
                    if cached_url:
                        consolidated_movies[key]['poster_url'] = cached_url
                    else:
                        poster_task = asyncio.create_task(get_poster_url(session, item['tmdb_id'], media_type))
                        poster_tasks.append((consolidated_movies[key], poster_task, media_type))
                else:
                    consolidated_movies[key]['versions'].append(item['version'])
                    consolidated_movies[key]['collected_at'] = max(consolidated_movies[key]['collected_at'], item['collected_at'])
                
                logging.debug(f"Consolidated movie: {key} - Versions: {consolidated_movies[key]['versions']}")
            
            # Process episodes
            for row in episode_results:
                item = dict(row)
                media_type = 'tv'
                
                if item['title'] not in shows:
                    show_item = {
                        'title': item['title'],
                        'year': item['year'],
                        'type': 'show',
                        'collected_at': item['collected_at'],
                        'imdb_id': item['imdb_id'],
                        'tmdb_id': item['tmdb_id'],
                        'seasons': [item['season_number']],
                        'latest_episode': (item['season_number'], item['episode_number']),
                        'versions': [item['version']]
                    }
                    cached_url = get_cached_poster_url(item['tmdb_id'], media_type)
                    if cached_url:
                        show_item['poster_url'] = cached_url
                    else:
                        poster_task = asyncio.create_task(get_poster_url(session, item['tmdb_id'], media_type))
                        poster_tasks.append((show_item, poster_task, media_type))
                    shows[item['title']] = show_item
                else:
                    show = shows[item['title']]
                    if item['season_number'] not in show['seasons']:
                        show['seasons'].append(item['season_number'])
                    show['collected_at'] = max(show['collected_at'], item['collected_at'])
                    show['latest_episode'] = max(show['latest_episode'], (item['season_number'], item['episode_number']))
                    if item['version'] not in show['versions']:
                        show['versions'].append(item['version'])
                
                logging.debug(f"Processed show: {item['title']} - Versions: {shows[item['title']]['versions']}")
            
            # Wait for all poster URL tasks to complete
            poster_results = await asyncio.gather(*[task for _, task, _ in poster_tasks], return_exceptions=True)
            
            # Assign poster URLs to items and cache them
            for (item, _, media_type), result in zip(poster_tasks, poster_results):
                if isinstance(result, Exception):
                    logging.error(f"Error fetching poster for {media_type} with TMDB ID {item['tmdb_id']}: {result}")
                elif result:
                    item['poster_url'] = result
                    cache_poster_url(item['tmdb_id'], media_type, result)
                else:
                    logging.warning(f"No poster URL found for {media_type} with TMDB ID {item['tmdb_id']}")

        
        # Convert consolidated_movies dict to list and sort
        movies_list = list(consolidated_movies.values())
        movies_list.sort(key=lambda x: x['collected_at'], reverse=True)
        
        # Convert shows dict to list and sort
        shows_list = list(shows.values())
        shows_list.sort(key=lambda x: x['collected_at'], reverse=True)
        
        logging.debug("Before limit_and_process:")
        for movie in movies_list:
            logging.debug(f"Movie: {movie['title']} ({movie['year']}) - Versions: {movie['versions']}")
        for show in shows_list:
            logging.debug(f"Show: {show['title']} - Versions: {show['versions']}")
        
        # Final processing and limiting to 5 unique items based on title
        def limit_and_process(items, limit=5):
            unique_items = {}
            for item in items:
                if len(unique_items) >= limit:
                    break
                if item['title'] not in unique_items:
                    if 'seasons' in item:
                        item['seasons'].sort()
                    item['versions'].sort()
                    item['versions'] = ', '.join(item['versions'])  # Join versions into a string
                    unique_items[item['title']] = item
                logging.debug(f"Processed item: {item['title']} - Versions: {item['versions']}")
            return list(unique_items.values())

        movies_list = limit_and_process(movies_list)
        shows_list = limit_and_process(shows_list)

        logging.debug("After limit_and_process:")
        for movie in movies_list:
            logging.debug(f"Movie: {movie['title']} ({movie['year']}) - Versions: {movie['versions']}")
        for show in shows_list:
            logging.debug(f"Show: {show['title']} - Versions: {show['versions']}")

        # Clean expired cache entries
        clean_expired_cache()

        return {
            'movies': movies_list,
            'shows': shows_list
        }
    except Exception as e:
        logging.error(f"Error in get_recently_added_items: {str(e)}")
        return {'movies': [], 'shows': []}
    finally:
        conn.close()

async def get_poster_url(session, tmdb_id, media_type):
    from content_checkers.overseerr import get_overseerr_headers

    overseerr_url = get_setting('Overseerr', 'url', '').rstrip('/')
    overseerr_api_key = get_setting('Overseerr', 'api_key', '')
    
    if not overseerr_url or not overseerr_api_key:
        logging.warning("Overseerr URL or API key is missing")
        return None
    
    headers = get_overseerr_headers(overseerr_api_key)
    
    url = f"{overseerr_url}/api/v1/{media_type}/{tmdb_id}"
    
    try:
        async with session.get(url, headers=headers, timeout=10) as response:
            if response.status == 200:
                data = await response.json()
                poster_path = data.get('posterPath')
                if poster_path:
                    return f"https://image.tmdb.org/t/p/w300{poster_path}"
                else:
                    logging.warning(f"No poster path found for {media_type} with TMDB ID {tmdb_id}")
            else:
                logging.error(f"Overseerr API returned status {response.status} for {media_type} with TMDB ID {tmdb_id}")
                
    except ClientConnectorError as e:
        logging.error(f"Unable to connect to Overseerr: {e}")
    except ServerTimeoutError:
        logging.error(f"Timeout while connecting to Overseerr for {media_type} with TMDB ID {tmdb_id}")
    except ClientResponseError as e:
        logging.error(f"Overseerr API error: {e}")
    except asyncio.TimeoutError:
        logging.error(f"Request to Overseerr timed out for {media_type} with TMDB ID {tmdb_id}")
    except Exception as e:
        logging.error(f"Unexpected error fetching poster URL for {media_type} with TMDB ID {tmdb_id}: {e}")
    
    return None