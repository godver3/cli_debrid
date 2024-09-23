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
        # Check if the column exists
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(media_items)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'original_collected_at' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN original_collected_at TIMESTAMP')
            logging.info("Successfully added original_collected_at column to media_items table.")
        if 'runtime' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN runtime INTEGER')
            logging.info("Successfully added runtime column to media_items table.")
        if 'alternate_title' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN alternate_title TEXT')
            logging.info("Successfully added alternate_title column to media_items table.")
        if 'airtime' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN airtime TIMESTAMP')
            logging.info("Successfully added airtime column to media_items table.")
        if 'original_collected_at' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN original_collected_at TIMESTAMP')
            logging.info("Successfully added original_collected_at column to media_items table.")

        logging.info("Successfully added new columns to media_items table.")

        # Remove the existing index if it exists
        conn.execute('DROP INDEX IF EXISTS unique_media_item_file')

        # Don't recreate the unique index
        # Instead, you might want to create a non-unique index for performance
        conn.execute('''
            CREATE INDEX IF NOT EXISTS media_item_file_index 
            ON media_items (imdb_id, tmdb_id, title, year, season_number, episode_number, version, filled_by_file)
            WHERE filled_by_file IS NOT NULL
        ''')

        conn.commit()
        logging.info("Schema migration completed successfully. Unique constraint removed.")
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
                original_collected_at TIMESTAMP,
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
                file_path TEXT,
                runtime INTEGER,  -- Add the runtime column
                alternate_title TEXT
            )
        ''')

        conn.commit()
        logging.info("Tables created successfully.")
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

        trakt_cache_file = '/user/db_content/trakt_last_activity.pkl'
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
