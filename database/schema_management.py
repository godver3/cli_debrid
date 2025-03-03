import logging
from .core import get_db_connection
from .torrent_tracking import create_torrent_tracking_table
import sqlite3
import os


def create_database():
    create_tables()
    create_torrent_tracking_table()
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
        if 'upgrading_from' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN upgrading_from TEXT')
            logging.info("Successfully added upgrading_from column to media_items table.")
        if 'blacklisted_date' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN blacklisted_date TIMESTAMP')
            logging.info("Successfully added blacklisted_date column to media_items table.")
        if 'location_on_disk' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN location_on_disk TEXT')
            logging.info("Successfully added location_on_disk column to media_items table.")
        if 'upgraded' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN upgraded BOOLEAN DEFAULT FALSE')
            logging.info("Successfully added upgraded column to media_items table.")
        if 'early_release' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN early_release BOOLEAN DEFAULT FALSE')
            logging.info("Successfully added early_release column to media_items table.")
        if 'original_path_for_symlink' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN original_path_for_symlink TEXT')
            logging.info("Successfully added original_path_for_symlink column to media_items table.")
        if 'original_scraped_torrent_title' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN original_scraped_torrent_title TEXT')
            logging.info("Successfully added original_scraped_torrent_title column to media_items table.")
        if 'upgrading_from_torrent_id' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN upgrading_from_torrent_id TEXT')
            logging.info("Successfully added upgrading_from_torrent_id column to media_items table.")
        if 'country' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN country TEXT')
            logging.info("Successfully added country column to media_items table.")
        if 'trigger_is_anime' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN trigger_is_anime BOOLEAN DEFAULT FALSE')
            logging.info("Successfully added trigger_is_anime column to media_items table.")
        if 'trigger_is_sports' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN trigger_is_sports BOOLEAN DEFAULT FALSE')
            logging.info("Successfully added trigger_is_sports column to media_items table.")
        if 'trigger_is_movie' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN trigger_is_movie BOOLEAN DEFAULT FALSE')
            logging.info("Successfully added trigger_is_movie column to media_items table.")
        if 'trigger_is_tv' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN trigger_is_tv BOOLEAN DEFAULT FALSE')
            logging.info("Successfully added trigger_is_tv column to media_items table.")
        if 'trigger_release_year' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN trigger_release_year INTEGER')
            logging.info("Successfully added trigger_release_year column to media_items table.")
        if 'trigger_genres' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN trigger_genres TEXT')
            logging.info("Successfully added trigger_genres column to media_items table.")
        if 'trigger_content_source' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN trigger_content_source TEXT')
            logging.info("Successfully added trigger_content_source column to media_items table.")
        if 'trigger_version' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN trigger_version TEXT')
            logging.info("Successfully added trigger_version column to media_items table.")
        if 'trigger_country' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN trigger_country TEXT')
            logging.info("Successfully added trigger_country column to media_items table.")
        if 'anime_format' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN anime_format TEXT')
            logging.info("Successfully added anime_format column to media_items table.")
        if 'fall_back_to_single_scraper' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN fall_back_to_single_scraper BOOLEAN DEFAULT FALSE')
            logging.info("Successfully added fall_back_to_single_scraper column to media_items table.")
        if 'preferred_alias' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN preferred_alias TEXT')
            logging.info("Successfully added preferred_alias column to media_items table.")
        if 'upgrading' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN upgrading BOOLEAN DEFAULT FALSE')
            logging.info("Successfully added upgrading column to media_items table.")
        if 'requested_season' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN requested_season BOOLEAN DEFAULT FALSE')
            logging.info("Successfully added requested_season column to media_items table.")
        if 'content_source' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN content_source TEXT')
            logging.info("Successfully added content_source column to media_items table.")
        if 'resolution' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN resolution TEXT')
            logging.info("Successfully added resolution column to media_items table.")
        if 'imdb_aliases' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN imdb_aliases TEXT')
            logging.info("Successfully added imdb_aliases column to media_items table.")
        if 'title_aliases' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN title_aliases TEXT')
            logging.info("Successfully added title_aliases column to media_items table.")
        if 'disable_not_wanted_check' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN disable_not_wanted_check BOOLEAN DEFAULT FALSE')
            logging.info("Successfully added disable_not_wanted_check column to media_items table.")
        if 'content_source_detail' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN content_source_detail TEXT')
            logging.info("Successfully added content_source_detail column to media_items table.")
        if 'physical_release_date' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN physical_release_date DATE')
            logging.info("Successfully added physical_release_date column to media_items table.")
        if 'plex_verified' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN plex_verified BOOLEAN DEFAULT FALSE')
            logging.info("Successfully added plex_verified column to media_items table.")
        
        # Check if symlinked_files_verification table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='symlinked_files_verification'")
        if not cursor.fetchone():
            conn.execute('''
                CREATE TABLE IF NOT EXISTS symlinked_files_verification (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    media_item_id INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    full_path TEXT NOT NULL,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    verified BOOLEAN DEFAULT FALSE,
                    verified_at TIMESTAMP,
                    verification_attempts INTEGER DEFAULT 0,
                    last_attempt TIMESTAMP,
                    FOREIGN KEY (media_item_id) REFERENCES media_items (id)
                )
            ''')
            logging.info("Successfully created symlinked_files_verification table.")

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
        # logging.info("Schema migration completed successfully. Unique constraint removed.")
    except Exception as e:
        conn.rollback()
        logging.error(f"Unexpected error during schema migration: {str(e)}")
    finally:
        conn.close()

def verify_database():
    create_tables()
    migrate_schema()
    create_torrent_tracking_table()
    
    # Add statistics indexes
    from .migrations import add_statistics_indexes
    add_statistics_indexes()
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Verify media_items table
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='media_items'")
    if not cursor.fetchone():
        logging.error("media_items table does not exist!")
        
    # Verify torrent_additions table
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='torrent_additions'")
    if not cursor.fetchone():
        logging.error("torrent_additions table does not exist!")
        
    conn.close()
    
    db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    db_path = os.path.join(db_content_dir, 'media_items.db')
    #logging.info(f"Successfully connected to cli_debrid database: sqlite:///{db_path}")


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
                runtime INTEGER,
                alternate_title TEXT,
                upgrading_from TEXT,
                blacklisted_date TIMESTAMP,
                upgraded BOOLEAN DEFAULT FALSE,
                location_on_disk TEXT,
                early_release BOOLEAN DEFAULT FALSE,
                original_path_for_symlink TEXT,
                original_scraped_torrent_title TEXT,
                upgrading_from_torrent_id TEXT,
                country TEXT,
                trigger_is_anime BOOLEAN DEFAULT FALSE,
                trigger_is_sports BOOLEAN DEFAULT FALSE,
                trigger_is_movie BOOLEAN DEFAULT FALSE,
                trigger_is_tv BOOLEAN DEFAULT FALSE,
                trigger_release_year INTEGER,
                trigger_genres TEXT,
                trigger_content_source TEXT,
                trigger_version TEXT,
                trigger_country TEXT,
                anime_format TEXT,
                fall_back_to_single_scraper BOOLEAN DEFAULT FALSE,
                preferred_alias TEXT,
                upgrading BOOLEAN DEFAULT FALSE,
                requested_season BOOLEAN DEFAULT FALSE,
                content_source TEXT,
                content_source_detail TEXT,
                resolution TEXT,
                imdb_aliases TEXT,
                title_aliases TEXT,
                disable_not_wanted_check BOOLEAN DEFAULT FALSE,
                physical_release_date DATE,
                plex_verified BOOLEAN DEFAULT FALSE
            )
        ''')

        # Add new table for tracking requested seasons
        conn.execute('''
            CREATE TABLE IF NOT EXISTS show_requested_seasons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                imdb_id TEXT NOT NULL,
                tmdb_id TEXT,
                season_number INTEGER NOT NULL,
                requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(imdb_id, season_number)
            )
        ''')
        
        # Add new table for tracking symlinked files for Plex verification
        conn.execute('''
            CREATE TABLE IF NOT EXISTS symlinked_files_verification (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                media_item_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                full_path TEXT NOT NULL,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                verified BOOLEAN DEFAULT FALSE,
                verified_at TIMESTAMP,
                verification_attempts INTEGER DEFAULT 0,
                last_attempt TIMESTAMP,
                FOREIGN KEY (media_item_id) REFERENCES media_items (id)
            )
        ''')

        conn.commit()
        # logging.info("Tables created successfully.")
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

        # Get db_content directory from environment variable with fallback
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        trakt_cache_file = os.path.join(db_content_dir, 'trakt_last_activity.pkl')
        
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
