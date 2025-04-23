import logging
from .core import get_db_connection, initialize_notifications_table
from .torrent_tracking import create_torrent_tracking_table
import sqlite3
import os


def create_database():
    create_tables()
    create_torrent_tracking_table()
    #TODO: create_upgrading_table()
    
    # Add statistics-specific indexes
    create_statistics_indexes()
    
    # Create materialized views for statistics
    create_statistics_summary_table()

def migrate_schema():
    conn = get_db_connection()
    try:
        # Initialize notifications table (idempotent)
        initialize_notifications_table(conn)
        logging.info("Checked/Initialized notifications table.")

        # Check if the column exists
        cursor = conn.cursor()
        
        # Check if statistics_summary table exists and has id column
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='statistics_summary'")
        if cursor.fetchone():
            cursor.execute("PRAGMA table_info(statistics_summary)")
            columns = [column[1] for column in cursor.fetchall()]
            if 'id' not in columns:
                # Create temporary table with new schema
                cursor.execute('''
                    CREATE TABLE statistics_summary_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        total_movies INTEGER NOT NULL DEFAULT 0,
                        total_shows INTEGER NOT NULL DEFAULT 0,
                        total_episodes INTEGER NOT NULL DEFAULT 0,
                        last_updated DATETIME NOT NULL,
                        latest_movie_collected DATETIME,
                        latest_episode_collected DATETIME,
                        latest_upgraded DATETIME,
                        latest_movie_collected_at DATETIME,
                        latest_episode_collected_at DATETIME,
                        latest_upgrade_at DATETIME
                    )
                ''')
                # Copy data from old table to new table
                cursor.execute('''
                    INSERT INTO statistics_summary_new 
                    (total_movies, total_shows, total_episodes, last_updated, 
                     latest_movie_collected, latest_episode_collected, latest_upgraded,
                     latest_movie_collected_at, latest_episode_collected_at, latest_upgrade_at)
                    SELECT total_movies, total_shows, total_episodes, last_updated,
                           latest_movie_collected_at, latest_episode_collected_at, latest_upgrade_at,
                           latest_movie_collected_at, latest_episode_collected_at, latest_upgrade_at 
                    FROM statistics_summary
                ''')
                # Drop old table and rename new table
                cursor.execute('DROP TABLE statistics_summary')
                cursor.execute('ALTER TABLE statistics_summary_new RENAME TO statistics_summary')
                logging.info("Successfully added id column and updated statistics_summary table.")
            else:
                # Add any missing columns
                if 'latest_movie_collected' not in columns:
                    conn.execute('ALTER TABLE statistics_summary ADD COLUMN latest_movie_collected DATETIME')
                    conn.execute('UPDATE statistics_summary SET latest_movie_collected = latest_movie_collected_at')
                    logging.info("Successfully added latest_movie_collected column to statistics_summary table.")
                if 'latest_episode_collected' not in columns:
                    conn.execute('ALTER TABLE statistics_summary ADD COLUMN latest_episode_collected DATETIME')
                    conn.execute('UPDATE statistics_summary SET latest_episode_collected = latest_episode_collected_at')
                    logging.info("Successfully added latest_episode_collected column to statistics_summary table.")
                if 'latest_upgraded' not in columns:
                    conn.execute('ALTER TABLE statistics_summary ADD COLUMN latest_upgraded DATETIME')
                    conn.execute('UPDATE statistics_summary SET latest_upgraded = latest_upgrade_at')
                    logging.info("Successfully added latest_upgraded column to statistics_summary table.")
                if 'latest_movie_collected_at' not in columns:
                    conn.execute('ALTER TABLE statistics_summary ADD COLUMN latest_movie_collected_at DATETIME')
                    conn.execute('UPDATE statistics_summary SET latest_movie_collected_at = latest_movie_collected')
                    logging.info("Successfully added latest_movie_collected_at column to statistics_summary table.")
                if 'latest_episode_collected_at' not in columns:
                    conn.execute('ALTER TABLE statistics_summary ADD COLUMN latest_episode_collected_at DATETIME')
                    conn.execute('UPDATE statistics_summary SET latest_episode_collected_at = latest_episode_collected')
                    logging.info("Successfully added latest_episode_collected_at column to statistics_summary table.")
                if 'latest_upgrade_at' not in columns:
                    conn.execute('ALTER TABLE statistics_summary ADD COLUMN latest_upgrade_at DATETIME')
                    conn.execute('UPDATE statistics_summary SET latest_upgrade_at = latest_upgraded')
                    logging.info("Successfully added latest_upgrade_at column to statistics_summary table.")
        
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
        if 'wake_count' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN wake_count INTEGER DEFAULT 0')
            logging.info("Successfully added wake_count column to media_items table.")
        if 'upgrading_from_version' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN upgrading_from_version TEXT')
            logging.info("Successfully added upgrading_from_version column to media_items table.")
        if 'no_early_release' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN no_early_release BOOLEAN DEFAULT FALSE')
            logging.info("Successfully added no_early_release column to media_items table.")
        if 'current_score' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN current_score REAL DEFAULT 0')
            logging.info("Successfully added current_score column to media_items table.")
        if 'final_check_add_timestamp' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN final_check_add_timestamp TIMESTAMP')
            logging.info("Successfully added final_check_add_timestamp column to media_items table.")

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

        # Add new table for tracking tv shows
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tv_shows (
                imdb_id TEXT PRIMARY KEY,
                tmdb_id TEXT UNIQUE,
                title TEXT,
                year INTEGER,
                status TEXT,
                is_complete INTEGER NOT NULL DEFAULT 0,
                total_episodes INTEGER,
                last_status_check TEXT,
                added_at TEXT,
                last_updated TEXT
            )
        ''')

        # Optional: Add indexes for tv_shows if needed
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_tv_shows_tmdb_id ON tv_shows (tmdb_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_tv_shows_status ON tv_shows (status)')

        # Add new table for tracking tv_show_version_status (logic adjusted previously)
        # Create the table if it doesn't exist
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tv_show_version_status (
                imdb_id TEXT NOT NULL,
                version_identifier TEXT NOT NULL, -- Will store the 'version' key value
                is_complete_and_present INTEGER NOT NULL DEFAULT 0, -- 1 for true, 0 for false
                present_episode_count INTEGER NOT NULL DEFAULT 0,
                is_up_to_date INTEGER NOT NULL DEFAULT 0,
                last_checked TEXT NOT NULL,
                PRIMARY KEY (imdb_id, version_identifier),
                FOREIGN KEY (imdb_id) REFERENCES tv_shows(imdb_id) ON DELETE CASCADE
            )
        ''')

        # Check if the tv_show_version_status table exists and add missing columns
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tv_show_version_status'")
        if cursor.fetchone():
            # Check if the 'is_up_to_date' column exists
            cursor.execute("PRAGMA table_info(tv_show_version_status)")
            columns = [column[1] for column in cursor.fetchall()]
            if 'is_up_to_date' not in columns:
                cursor.execute('ALTER TABLE tv_show_version_status ADD COLUMN is_up_to_date INTEGER NOT NULL DEFAULT 0')
                logging.info("Successfully added is_up_to_date column to tv_show_version_status table.")
            # Add checks for other columns here if needed in the future

        conn.commit()
        logging.info("Schema migration checks completed successfully.")
    except Exception as e:
        conn.rollback()
        logging.error(f"Unexpected error during schema migration: {str(e)}", exc_info=True)
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
        cursor = conn.cursor() # Use a cursor for PRAGMA checks

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
                collected_at TIMESTAMP,
                original_collected_at TIMESTAMP,
                filled_by_file TEXT,
                filled_by_title TEXT,
                filled_by_magnet TEXT,
                filled_by_torrent_id TEXT,
                airtime TIMESTAMP,
                last_updated TIMESTAMP,
                metadata_updated TIMESTAMP,
                wake_count INTEGER DEFAULT 0,
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
                plex_verified BOOLEAN DEFAULT FALSE,
                upgrading_from_version TEXT,
                no_early_release BOOLEAN DEFAULT FALSE,
                current_score REAL DEFAULT 0,
                final_check_add_timestamp TIMESTAMP
            )
        ''')

        # Add new table for tracking requested seasons
        cursor.execute('''
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
        cursor.execute('''
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

        # Add new table for tracking tv shows
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tv_shows (
                imdb_id TEXT PRIMARY KEY,
                tmdb_id TEXT UNIQUE,
                title TEXT,
                year INTEGER,
                status TEXT,
                is_complete INTEGER NOT NULL DEFAULT 0,
                total_episodes INTEGER,
                last_status_check TEXT,
                added_at TEXT,
                last_updated TEXT
            )
        ''')

        # Optional: Add indexes for tv_shows if needed
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_tv_shows_tmdb_id ON tv_shows (tmdb_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_tv_shows_status ON tv_shows (status)')

        # Add new table for tracking tv_show_version_status (logic adjusted previously)
        # Create the table if it doesn't exist
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tv_show_version_status (
                imdb_id TEXT NOT NULL,
                version_identifier TEXT NOT NULL, -- Will store the 'version' key value
                is_complete_and_present INTEGER NOT NULL DEFAULT 0, -- 1 for true, 0 for false
                present_episode_count INTEGER NOT NULL DEFAULT 0,
                is_up_to_date INTEGER NOT NULL DEFAULT 0,
                last_checked TEXT NOT NULL,
                PRIMARY KEY (imdb_id, version_identifier),
                FOREIGN KEY (imdb_id) REFERENCES tv_shows(imdb_id) ON DELETE CASCADE
            )
        ''')

        # Check if the tv_show_version_status table exists and add missing columns
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tv_show_version_status'")
        if cursor.fetchone():
            # Check if the 'is_up_to_date' column exists
            cursor.execute("PRAGMA table_info(tv_show_version_status)")
            columns = [column[1] for column in cursor.fetchall()]
            if 'is_up_to_date' not in columns:
                cursor.execute('ALTER TABLE tv_show_version_status ADD COLUMN is_up_to_date INTEGER NOT NULL DEFAULT 0')
                logging.info("Successfully added is_up_to_date column to tv_show_version_status table.")
            # Add checks for other columns here if needed in the future

        conn.commit()
        # logging.info("Tables created successfully.")
    except Exception as e:
        logging.error(f"Error creating tables: {str(e)}")
        if conn:
            conn.rollback() # Rollback on error
    finally:
        if conn:
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

def create_statistics_indexes():
    """Create indexes specifically for statistics queries"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Drop existing indexes if they exist to allow recreation
    cursor.execute("DROP INDEX IF EXISTS idx_media_airing_improved")
    cursor.execute("DROP INDEX IF EXISTS idx_media_airing_prefilter")
    cursor.execute("DROP INDEX IF EXISTS idx_media_episodes_min_rowid")
    cursor.execute("DROP INDEX IF EXISTS idx_media_episodes_min_id")
    
    # Create specific index for release date filtering on episodes (helps with temp table creation)
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_media_airing_prefilter ON media_items (
        type, release_date, title
    ) WHERE type = 'episode'
    """)
    
    # Create index to help with the min id subquery
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_media_episodes_min_id ON media_items (
        title, season_number, episode_number, id
    ) WHERE type = 'episode'
    """)
    
    # Create more specific index for the main airing query with ordered columns
    cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_media_airing_improved ON media_items (
        type, title, release_date, airtime, season_number, episode_number, state
    ) WHERE type = 'episode'
    """)
    
    conn.commit()
    conn.close()

def create_statistics_summary_table():
    """Create the statistics summary table and its indexes"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # Create the statistics summary table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS statistics_summary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                total_movies INTEGER NOT NULL DEFAULT 0,
                total_shows INTEGER NOT NULL DEFAULT 0,
                total_episodes INTEGER NOT NULL DEFAULT 0,
                last_updated DATETIME NOT NULL,
                latest_movie_collected DATETIME,
                latest_episode_collected DATETIME,
                latest_upgraded DATETIME,
                latest_movie_collected_at DATETIME,
                latest_episode_collected_at DATETIME,
                latest_upgrade_at DATETIME
            )
        ''')
        
        # Add optimized indexes for recently added items
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_media_items_recent_movies
            ON media_items(collected_at DESC, type, state)
            WHERE collected_at IS NOT NULL
        ''')
        
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_media_items_recent_episodes
            ON media_items(collected_at DESC, type, state)
            WHERE collected_at IS NOT NULL
        ''')
        
        conn.commit()
    except Exception as e:
        logging.error(f"Error creating statistics summary table: {str(e)}")
        raise
    finally:
        conn.close()
