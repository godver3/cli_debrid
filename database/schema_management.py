import logging
from .core import get_db_connection
import sqlite3
import os


def create_database():
    create_tables()
    #TODO: create_upgrading_table()

def migrate_schema():
    conn = get_db_connection()
    try:
        # Drop the old unique constraint if it exists
        try:
            conn.execute('DROP INDEX IF EXISTS media_items_unique_constraint')
            logging.info("Successfully dropped old unique constraint.")
        except sqlite3.OperationalError as e:
            logging.error(f"Error dropping old unique constraint: {str(e)}")

        # Create new unique index
        conn.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS unique_media_item_file 
            ON media_items (imdb_id, tmdb_id, title, year, season_number, episode_number, version, filled_by_file)
            WHERE filled_by_file IS NOT NULL
        ''')
        logging.info("Successfully created new unique index.")

        conn.commit()
        logging.info("Schema migration completed successfully.")
    except Exception as e:
        conn.rollback()
        logging.error(f"Unexpected error during schema migration: {str(e)}")
    finally:
        conn.close()

def verify_database():
    create_tables()
    migrate_schema()
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Verify media_items table
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='media_items'")
    if not cursor.fetchone():
        logging.error("media_items table does not exist!")
    
    # Verify filled_by_file column
    cursor.execute("PRAGMA table_info(media_items)")
    columns = [column[1] for column in cursor.fetchall()]
    if 'filled_by_file' not in columns:
        logging.error("filled_by_file column does not exist in media_items table!")
    
    # Verify unique index
    cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='unique_media_item_file'")
    if not cursor.fetchone():
        logging.error("unique_media_item_file index does not exist!")
    
    conn.close()
    
    logging.info("Database verification complete.")

def create_tables():
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
                collected_at TIMESTAMP,
                filled_by_file TEXT,
                filled_by_title TEXT,
                filled_by_magnet TEXT,
                filled_by_torrent_id TEXT,
                airtime TIMESTAMP,
                last_updated TIMESTAMP,
                metadata_updated TIMESTAMP,
                sleep_cycles INTEGER DEFAULT 0,
                last_checked TIMESTAMP,
                scrape_results TEXT,
                version TEXT,
                genres TEXT,
                file_path TEXT
            )
        ''')

        conn.commit()
    except Exception as e:
        logging.error(f"Error creating media_items table: {str(e)}")
    finally:
        conn.close()

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
                filled_by_file TEXT,
                filled_by_title TEXT,
                filled_by_magnet TEXT,
                filled_by_torrent_id TEXT,
                last_updated TIMESTAMP,
                metadata_updated TIMESTAMP,
                airtime TIMESTAMP,
                sleep_cycles INTEGER DEFAULT 0,
                last_checked TIMESTAMP,
                scrape_results TEXT,
                version TEXT,
                collected_at TIMESTAMP,
                genres TEXT,
                UNIQUE(imdb_id, tmdb_id, title, year, season_number, episode_number, version)
            )
        ''')

        # Step 2: Copy data from the old table to the new one
        cursor.execute('''
            INSERT INTO media_items_new 
            (imdb_id, tmdb_id, title, year, release_date, state, type, episode_title, 
             season_number, episode_number, filled_by_file, filled_by_title, filled_by_magnet, 
             last_updated, metadata_updated, airtime, sleep_cycles, last_checked, scrape_results, version,
             collected_at, genres)
            SELECT 
                imdb_id, tmdb_id, title, year, release_date, state, type, episode_title, 
                season_number, episode_number, filled_by_file, filled_by_title, filled_by_magnet, 
                last_updated, metadata_updated, sleep_cycles, last_checked, scrape_results, 
                COALESCE(version, 'default'),
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