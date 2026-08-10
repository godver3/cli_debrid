import logging
from .core import get_db_connection, initialize_notifications_table
from .torrent_tracking import create_torrent_tracking_table
from .content_source_retry import create_retry_queue_table
from .upgrade_hub_activity import create_upgrade_hub_activity_table
from .nzb_repair_activity import create_nzb_repair_activity_table
import sqlite3
import os


def create_database():
    create_tables()
    create_torrent_tracking_table()
    create_retry_queue_table()
    create_upgrade_hub_activity_table()
    create_nzb_repair_activity_table()
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
        if 'size' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN size REAL')
            logging.info("Successfully added size column to media_items table.")
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
        if 'content_sources' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN content_sources TEXT')
            logging.info("Successfully added content_sources column to media_items table.")
        if 'plex_labels' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN plex_labels TEXT')
            logging.info("Successfully added plex_labels column to media_items table.")
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
        if 'real_debrid_original_title' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN real_debrid_original_title TEXT')
            logging.info("Successfully added real_debrid_original_title column to media_items table.")
        if 'rescrape_original_torrent_title' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN rescrape_original_torrent_title TEXT')
            logging.info("Successfully added rescrape_original_torrent_title column to media_items table.")
        if 'force_priority' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN force_priority BOOLEAN DEFAULT FALSE')
            logging.info("Successfully added force_priority column to media_items table.")
        if 'location_basename' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN location_basename TEXT')
            logging.info("Successfully added location_basename column to media_items table.")
        if 'ghostlisted' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN ghostlisted BOOLEAN DEFAULT FALSE')
            logging.info("Successfully added ghostlisted column to media_items table.")
        if 'theatrical_release_date' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN theatrical_release_date DATE')
            logging.info("Successfully added theatrical_release_date column to media_items table.")
        if 'theatrical_release_date_checked' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN theatrical_release_date_checked BOOLEAN DEFAULT FALSE')
            logging.info("Successfully added theatrical_release_date_checked column to media_items table.")
        if 'delayed_upgrade_eligible' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN delayed_upgrade_eligible BOOLEAN DEFAULT TRUE')
            logging.info("Successfully added delayed_upgrade_eligible column to media_items table (default TRUE).")
        if 'verification_failed' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN verification_failed BOOLEAN DEFAULT FALSE')
            logging.info("Successfully added verification_failed column to media_items table.")
        if 'verification_failure_reason' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN verification_failure_reason TEXT')
            logging.info("Successfully added verification_failure_reason column to media_items table.")
        if 'plex_labels_last_synced' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN plex_labels_last_synced TIMESTAMP')
            logging.info("Successfully added plex_labels_last_synced column to media_items table.")
        if 'selected_folder' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN selected_folder TEXT')
            logging.info("Successfully added selected_folder column to media_items table.")
        if 'selected_folder_is_custom' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN selected_folder_is_custom BOOLEAN DEFAULT FALSE')
            logging.info("Successfully added selected_folder_is_custom column to media_items table.")
        if 'tags' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN tags TEXT')
            logging.info("Successfully added tags column to media_items table.")
        if 'tags_pushed_at' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN tags_pushed_at TIMESTAMP')
            logging.info("Successfully added tags_pushed_at column to media_items table.")
        if 'manual_replace' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN manual_replace BOOLEAN DEFAULT FALSE')
            logging.info("Successfully added manual_replace column to media_items table.")
        if 'debrid_folder_name' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN debrid_folder_name TEXT')
            logging.info("Successfully added debrid_folder_name column to media_items table.")

        # ============================================
        # Overlay System Tables
        # ============================================
        # Overlay tables are initialized via overlays/db_init.py (called in verify_database below)
        # Legacy plex_* overlay columns (kept for backward compatibility, no longer written to)
        if 'plex_rating_key' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN plex_rating_key TEXT')
            logging.info("Successfully added plex_rating_key column to media_items table.")
        if 'plex_resolution' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN plex_resolution TEXT')
        if 'plex_hdr' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN plex_hdr INTEGER DEFAULT 0')
        if 'plex_dolby_vision' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN plex_dolby_vision INTEGER DEFAULT 0')
        if 'plex_hdr_format' not in columns:
            conn.execute("ALTER TABLE media_items ADD COLUMN plex_hdr_format TEXT DEFAULT NULL")
        if 'plex_audio_codec' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN plex_audio_codec TEXT')
        if 'plex_audio_channels' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN plex_audio_channels TEXT')
        if 'plex_video_codec' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN plex_video_codec TEXT')
        if 'plex_media_container' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN plex_media_container TEXT')
        if 'plex_media_bitrate' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN plex_media_bitrate INTEGER')
        if 'plex_last_scanned' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN plex_last_scanned TIMESTAMP')
        if 'plex_network' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN plex_network TEXT')
        if 'plex_studio' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN plex_studio TEXT')
        if 'plex_content_rating' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN plex_content_rating TEXT')

        # Media-server-agnostic overlay columns (ms_* = media server, works for Plex and Jellyfin/Emby)
        if 'ms_item_id' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN ms_item_id TEXT')
            logging.info("Added ms_item_id column (media-server-agnostic overlay key).")
        if 'ms_resolution' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN ms_resolution TEXT')
            logging.info("Added ms_resolution column.")
        if 'ms_hdr' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN ms_hdr INTEGER DEFAULT 0')
            logging.info("Added ms_hdr column.")
        if 'ms_dolby_vision' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN ms_dolby_vision INTEGER DEFAULT 0')
            logging.info("Added ms_dolby_vision column.")
        if 'ms_hdr_format' not in columns:
            conn.execute("ALTER TABLE media_items ADD COLUMN ms_hdr_format TEXT DEFAULT NULL")
            logging.info("Added ms_hdr_format column.")
        if 'plex_guid' not in columns:
            conn.execute("ALTER TABLE media_items ADD COLUMN plex_guid TEXT DEFAULT NULL")
            logging.info("Added plex_guid column to media_items.")
        if 'ms_audio_codec' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN ms_audio_codec TEXT')
            logging.info("Added ms_audio_codec column.")
        if 'ms_audio_channels' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN ms_audio_channels TEXT')
            logging.info("Added ms_audio_channels column.")
        if 'ms_video_codec' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN ms_video_codec TEXT')
            logging.info("Added ms_video_codec column.")
        if 'ms_media_container' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN ms_media_container TEXT')
            logging.info("Added ms_media_container column.")
        if 'ms_media_bitrate' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN ms_media_bitrate INTEGER')
            logging.info("Added ms_media_bitrate column.")
        if 'ms_last_scanned' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN ms_last_scanned TIMESTAMP')
            logging.info("Added ms_last_scanned column.")
        if 'ms_network' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN ms_network TEXT')
            logging.info("Added ms_network column.")
        if 'ms_studio' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN ms_studio TEXT')
            logging.info("Added ms_studio column.")
        if 'ms_content_rating' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN ms_content_rating TEXT')
            logging.info("Added ms_content_rating column.")
        if 'ms_audio_track' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN ms_audio_track TEXT')
            logging.info("Added ms_audio_track column.")
        if 'ms_subtitle_track' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN ms_subtitle_track TEXT')
            logging.info("Added ms_subtitle_track column.")
        if 'source_position' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN source_position INTEGER')
            logging.info("Added source_position column to media_items table.")
        if 'tmdb_collection_id' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN tmdb_collection_id TEXT')
            logging.info("Added tmdb_collection_id column to media_items table.")
        if 'tmdb_collection_name' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN tmdb_collection_name TEXT')
            logging.info("Added tmdb_collection_name column to media_items table.")
        if 'nzb_segment_id' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN nzb_segment_id TEXT')
            logging.info("Added nzb_segment_id column to media_items table.")
        if 'original_filename' not in columns:
            conn.execute('ALTER TABLE media_items ADD COLUMN original_filename TEXT')
            logging.info("Added original_filename column to media_items table.")

        # Migrate data from legacy plex_* columns to ms_* columns (one-time migration)
        # Only runs when plex_rating_key data exists AND ms_item_id is completely unpopulated
        # (i.e. no rows have any ms_item_id at all yet). This prevents re-stamping Plex integer
        # IDs over NULLs that were intentionally cleared by the overlay system (e.g. stale key
        # resets in Jellyfin mode, or manual sync that hasn't run yet).
        ms_count_row = conn.execute('SELECT COUNT(*) FROM media_items WHERE ms_item_id IS NOT NULL AND ms_item_id != ""').fetchone()
        ms_populated = ms_count_row[0] if ms_count_row else 0
        if ms_populated == 0:
            conn.execute('''
                UPDATE media_items
                SET ms_item_id = plex_rating_key,
                    ms_resolution = plex_resolution,
                    ms_hdr = plex_hdr,
                    ms_dolby_vision = plex_dolby_vision,
                    ms_hdr_format = plex_hdr_format,
                    ms_audio_codec = plex_audio_codec,
                    ms_audio_channels = plex_audio_channels,
                    ms_video_codec = plex_video_codec,
                    ms_media_container = plex_media_container,
                    ms_media_bitrate = plex_media_bitrate,
                    ms_last_scanned = plex_last_scanned,
                    ms_network = plex_network,
                    ms_studio = plex_studio,
                    ms_content_rating = plex_content_rating
                WHERE plex_rating_key IS NOT NULL
                  AND (ms_item_id IS NULL OR ms_item_id = '')
            ''')

        logging.info("Overlay system migration complete (ms_* columns ready).")

        # Add new indexes for version and content_source if they don't exist
        existing_indexes_cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='index';")
        existing_indexes = [row[0] for row in existing_indexes_cursor.fetchall()]

        if 'idx_media_items_version' not in existing_indexes:
            logging.info("Attempting to create index idx_media_items_version...")
            conn.execute('CREATE INDEX idx_media_items_version ON media_items(version);')
            logging.info("Successfully executed CREATE INDEX for idx_media_items_version.")
        
        if 'idx_media_items_content_source' not in existing_indexes:
            logging.info("Attempting to create index idx_media_items_content_source...")
            conn.execute('CREATE INDEX idx_media_items_content_source ON media_items(content_source);')
            logging.info("Successfully executed CREATE INDEX for idx_media_items_content_source.")

        if 'idx_media_items_location_basename' not in existing_indexes:
            logging.info("Attempting to create index idx_media_items_location_basename...")
            conn.execute('CREATE INDEX idx_media_items_location_basename ON media_items(location_basename);')
            logging.info("Successfully created CREATE INDEX for idx_media_items_location_basename.")

        # Add index for original_path_for_symlink if it doesn't exist
        if 'idx_media_items_original_path_for_symlink' not in existing_indexes:
            logging.info("Attempting to create index idx_media_items_original_path_for_symlink...")
            conn.execute('CREATE INDEX idx_media_items_original_path_for_symlink ON media_items(original_path_for_symlink);')
            logging.info("Successfully executed CREATE INDEX for idx_media_items_original_path_for_symlink.")

        # Add index for collected_at to fix slow queries in get_items_processed_per_hour
        if 'idx_media_items_collected_at' not in existing_indexes:
            logging.info("Attempting to create index idx_media_items_collected_at...")
            conn.execute('CREATE INDEX idx_media_items_collected_at ON media_items(collected_at);')
            logging.info("Successfully executed CREATE INDEX for idx_media_items_collected_at.")

        # Add indexes for optimizing get_episode_runtime and get_episode_count
        if 'idx_media_items_tmdb_type_runtime' not in existing_indexes:
            logging.info("Attempting to create index idx_media_items_tmdb_type_runtime...")
            conn.execute('CREATE INDEX IF NOT EXISTS idx_media_items_tmdb_type_runtime ON media_items(tmdb_id, type, runtime);')
            logging.info("Successfully executed CREATE INDEX for idx_media_items_tmdb_type_runtime.")

        if 'idx_media_items_tmdb_type_ep_info' not in existing_indexes:
            logging.info("Attempting to create index idx_media_items_tmdb_type_ep_info...")
            conn.execute('CREATE INDEX IF NOT EXISTS idx_media_items_tmdb_type_ep_info ON media_items(tmdb_id, type, season_number, episode_number, version);')
            logging.info("Successfully executed CREATE INDEX for idx_media_items_tmdb_type_ep_info.")

        # Add indexes for get_season_year function performance
        if 'idx_media_items_imdb_type_season_release' not in existing_indexes:
            logging.info("Attempting to create index idx_media_items_imdb_type_season_release...")
            conn.execute('CREATE INDEX IF NOT EXISTS idx_media_items_imdb_type_season_release ON media_items(imdb_id, type, season_number, release_date);')
            logging.info("Successfully executed CREATE INDEX for idx_media_items_imdb_type_season_release.")

        if 'idx_media_items_tmdb_type_season_release' not in existing_indexes:
            logging.info("Attempting to create index idx_media_items_tmdb_type_season_release...")
            conn.execute('CREATE INDEX IF NOT EXISTS idx_media_items_tmdb_type_season_release ON media_items(tmdb_id, type, season_number, release_date);')
            logging.info("Successfully executed CREATE INDEX for idx_media_items_tmdb_type_season_release.")

        # New Suggested Index for get_distinct_library_shows
        if 'idx_media_items_imdb_type_state_title' not in existing_indexes:
            logging.info("Attempting to create index idx_media_items_imdb_type_state_title...")
            # This index helps with partitioning by imdb_id, filtering by type, and ordering by state and title.
            conn.execute('CREATE INDEX IF NOT EXISTS idx_media_items_imdb_type_state_title ON media_items(imdb_id, type, state, title COLLATE NOCASE);')
            logging.info("Successfully executed CREATE INDEX for idx_media_items_imdb_type_state_title.")

        # New index for get_collected_counts (shows)
        if 'idx_media_items_type_state_imdb' not in existing_indexes:
            logging.info("Attempting to create index idx_media_items_type_state_imdb...")
            conn.execute('CREATE INDEX IF NOT EXISTS idx_media_items_type_state_imdb ON media_items(type, state, imdb_id);')
            logging.info("Successfully executed CREATE INDEX for idx_media_items_type_state_imdb.")

        # New index for get_upcoming_releases
        if 'idx_media_items_upcoming_releases' not in existing_indexes:
            logging.info("Attempting to create index idx_media_items_upcoming_releases...")
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_media_items_upcoming_releases
                ON media_items(type, release_date)
                WHERE state = 'Wanted'
            ''')
            logging.info("Successfully executed CREATE INDEX for idx_media_items_upcoming_releases.")

        # Index for fast state-based queries (get_item_count_by_state)
        if 'idx_media_items_state' not in existing_indexes:
            logging.info("Attempting to create index idx_media_items_state...")
            conn.execute('CREATE INDEX IF NOT EXISTS idx_media_items_state ON media_items(state);')
            logging.info("Successfully executed CREATE INDEX for idx_media_items_state.")

        # Composite index for Blacklisted queries (state + ghostlisted for covering index)
        if 'idx_media_items_state_ghostlisted' not in existing_indexes:
            logging.info("Attempting to create index idx_media_items_state_ghostlisted...")
            conn.execute('CREATE INDEX IF NOT EXISTS idx_media_items_state_ghostlisted ON media_items(state, ghostlisted);')
            logging.info("Successfully executed CREATE INDEX for idx_media_items_state_ghostlisted.")

        # Composite index for database page sorting by content_source_detail
        # This helps with queries that filter by state and sort by content_source_detail
        if 'idx_media_items_type_state_content_source_detail' not in existing_indexes:
            logging.info("Attempting to create index idx_media_items_type_state_content_source_detail...")
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_media_items_type_state_content_source_detail
                ON media_items(type, state, content_source_detail)
            ''')
            logging.info("Successfully executed CREATE INDEX for idx_media_items_type_state_content_source_detail.")

        # Index for content_source_detail alone (helps with IS NULL / IS NOT NULL queries)
        if 'idx_media_items_content_source_detail' not in existing_indexes:
            logging.info("Attempting to create index idx_media_items_content_source_detail...")
            conn.execute('CREATE INDEX IF NOT EXISTS idx_media_items_content_source_detail ON media_items(content_source_detail);')
            logging.info("Successfully executed CREATE INDEX for idx_media_items_content_source_detail.")

        # Add triggers for location_basename
        cursor.execute("DROP TRIGGER IF EXISTS trigger_media_items_insert_location_basename")
        cursor.execute("DROP TRIGGER IF EXISTS trigger_media_items_update_location_basename")
        
        conn.execute('''
            CREATE TRIGGER trigger_media_items_insert_location_basename
            AFTER INSERT ON media_items
            FOR EACH ROW
            WHEN NEW.location_on_disk IS NOT NULL
              AND (NEW.filled_by_torrent_id IS NULL OR NEW.filled_by_torrent_id NOT LIKE 'nzb:%')
            BEGIN
                UPDATE media_items
                SET location_basename = REPLACE(NEW.location_on_disk, RTRIM(NEW.location_on_disk, REPLACE(NEW.location_on_disk, '/', '')), '')
                WHERE id = NEW.id;
            END;
        ''')
        logging.info("Successfully created insert trigger for location_basename.")
        
        conn.execute('''
            CREATE TRIGGER trigger_media_items_update_location_basename
            AFTER UPDATE OF location_on_disk ON media_items
            FOR EACH ROW
            WHEN NEW.location_on_disk IS NOT NULL
              AND (OLD.location_on_disk IS NULL OR NEW.location_on_disk != OLD.location_on_disk)
              AND (NEW.filled_by_torrent_id IS NULL OR NEW.filled_by_torrent_id NOT LIKE 'nzb:%')
            BEGIN
                UPDATE media_items
                SET location_basename = REPLACE(NEW.location_on_disk, RTRIM(NEW.location_on_disk, REPLACE(NEW.location_on_disk, '/', '')), '')
                WHERE id = NEW.id;
            END;
        ''')
        logging.info("Successfully created update trigger for location_basename.")

        # Backfill location_basename for existing data
        # Check if there are any rows that need backfilling
        cursor.execute("SELECT 1 FROM media_items WHERE location_on_disk IS NOT NULL AND location_basename IS NULL LIMIT 1")
        if cursor.fetchone():
            logging.info("Backfilling location_basename for existing media items...")
            conn.execute('''
                UPDATE media_items
                SET location_basename = REPLACE(location_on_disk, RTRIM(location_on_disk, REPLACE(location_on_disk, '/', '')), '')
                WHERE location_on_disk IS NOT NULL AND location_basename IS NULL;
            ''')
            logging.info("Finished backfilling location_basename.")

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
                    permanently_failed BOOLEAN DEFAULT FALSE,
                    failure_reason TEXT,
                    FOREIGN KEY (media_item_id) REFERENCES media_items (id)
                )
            ''')
            logging.info("Successfully created symlinked_files_verification table.")
        else:
            # If table exists, check for 'permanently_failed' column
            cursor.execute("PRAGMA table_info(symlinked_files_verification)")
            columns = [column[1] for column in cursor.fetchall()]
            if 'permanently_failed' not in columns:
                conn.execute('ALTER TABLE symlinked_files_verification ADD COLUMN permanently_failed BOOLEAN DEFAULT FALSE')
                logging.info("Successfully added permanently_failed column to symlinked_files_verification table.")
            if 'failure_reason' not in columns:
                conn.execute('ALTER TABLE symlinked_files_verification ADD COLUMN failure_reason TEXT')
                logging.info("Successfully added failure_reason column to symlinked_files_verification table.")

        # Fix future timestamps in statistics_summary table
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='statistics_summary'")
        if cursor.fetchone():
            # Check if there are any future timestamps in the statistics_summary table
            cursor.execute("""
                SELECT COUNT(*) FROM statistics_summary 
                WHERE last_updated > datetime('now', 'localtime')
            """)
            future_timestamps_count = cursor.fetchone()[0]
            
            if future_timestamps_count > 0:
                logging.warning(f"Found {future_timestamps_count} statistics_summary entries with future timestamps. Fixing...")
                
                # Reset future timestamps to current time
                cursor.execute("""
                    UPDATE statistics_summary 
                    SET last_updated = datetime('now', 'localtime')
                    WHERE last_updated > datetime('now', 'localtime')
                """)
                
                updated_rows = cursor.rowcount
                logging.info(f"Fixed {updated_rows} statistics_summary entries with future timestamps.")
            else:
                logging.debug("No future timestamps found in statistics_summary table.")
            
            # Also check and fix other timestamp fields that might have future dates
            cursor.execute("""
                SELECT COUNT(*) FROM statistics_summary 
                WHERE latest_movie_collected > datetime('now', 'localtime')
                   OR latest_episode_collected > datetime('now', 'localtime')
                   OR latest_upgraded > datetime('now', 'localtime')
            """)
            other_future_timestamps_count = cursor.fetchone()[0]
            
            if other_future_timestamps_count > 0:
                logging.warning(f"Found {other_future_timestamps_count} statistics_summary entries with future timestamps in other fields. Fixing...")
                
                # Reset future timestamps to NULL (since we don't know the correct historical time)
                cursor.execute("""
                    UPDATE statistics_summary 
                    SET latest_movie_collected = NULL
                    WHERE latest_movie_collected > datetime('now', 'localtime')
                """)
                
                cursor.execute("""
                    UPDATE statistics_summary 
                    SET latest_episode_collected = NULL
                    WHERE latest_episode_collected > datetime('now', 'localtime')
                """)
                
                cursor.execute("""
                    UPDATE statistics_summary 
                    SET latest_upgraded = NULL
                    WHERE latest_upgraded > datetime('now', 'localtime')
                """)
                
                logging.info("Fixed future timestamps in other statistics_summary fields.")
            else:
                logging.debug("No future timestamps found in other statistics_summary fields.")
            
            # Ensure statistics_summary table has at least one row
            cursor.execute("SELECT COUNT(*) FROM statistics_summary")
            row_count = cursor.fetchone()[0]
            
            if row_count == 0:
                logging.info("Statistics_summary table is empty. Creating initial row...")
                cursor.execute("""
                    INSERT INTO statistics_summary 
                    (id, total_movies, total_shows, total_episodes, last_updated)
                    VALUES (1, 0, 0, 0, datetime('now', 'localtime'))
                """)
                logging.info("Created initial statistics_summary row.")
            else:
                logging.debug(f"Statistics_summary table has {row_count} row(s).")

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

        # Add total_seasons, plex_guid, season_guids to tv_shows if missing
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tv_shows'")
        if cursor.fetchone():
            cursor.execute("PRAGMA table_info(tv_shows)")
            columns = [column[1] for column in cursor.fetchall()]
            if 'total_seasons' not in columns:
                cursor.execute('ALTER TABLE tv_shows ADD COLUMN total_seasons INTEGER')
                logging.info("Successfully added total_seasons column to tv_shows table.")
            if 'plex_guid' not in columns:
                cursor.execute('ALTER TABLE tv_shows ADD COLUMN plex_guid TEXT DEFAULT NULL')
                logging.info("Added plex_guid column to tv_shows table.")
            if 'season_guids' not in columns:
                cursor.execute('ALTER TABLE tv_shows ADD COLUMN season_guids TEXT DEFAULT NULL')
                logging.info("Added season_guids column to tv_shows table (JSON dict of season_number -> plex_guid).")

        # Rename plex_overlay_state → media_overlay_state (media-server-agnostic naming)
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='plex_overlay_state'")
        if cursor.fetchone():
            cursor.execute('ALTER TABLE plex_overlay_state RENAME TO media_overlay_state')
            logging.info("Renamed table plex_overlay_state → media_overlay_state.")

        # Add indexes on media_overlay_state (only if the table exists — it may not yet on fresh installs)
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='media_overlay_state'")
        if cursor.fetchone():
            # Add UNIQUE constraint on media_overlay_state.media_item_id (required for ON CONFLICT upsert)
            cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='uq_media_overlay_state_media_item_id'")
            if not cursor.fetchone():
                cursor.execute('''
                    CREATE UNIQUE INDEX uq_media_overlay_state_media_item_id
                    ON media_overlay_state(media_item_id)
                ''')
                logging.info("Added UNIQUE index on media_overlay_state(media_item_id).")

            # Add index on media_overlay_state(status) for fast status-based aggregation
            cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_media_overlay_state_status'")
            if not cursor.fetchone():
                cursor.execute('''
                    CREATE INDEX idx_media_overlay_state_status
                    ON media_overlay_state(status)
                ''')
                logging.info("Added index on media_overlay_state(status).")

        # Rename plex_removal_queue → overlay_removal_queue
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='plex_removal_queue'")
        if cursor.fetchone():
            # Only rename if the target name doesn't already exist
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='overlay_removal_queue'")
            if not cursor.fetchone():
                cursor.execute('ALTER TABLE plex_removal_queue RENAME TO overlay_removal_queue')
                logging.info("Renamed table plex_removal_queue → overlay_removal_queue.")
            else:
                cursor.execute('DROP TABLE plex_removal_queue')
                logging.info("Dropped stale plex_removal_queue (overlay_removal_queue already exists).")

        conn.execute('''
            CREATE TABLE IF NOT EXISTS plex_collection_sync (
                source_id TEXT PRIMARY KEY,
                movie_collection_ratingkey TEXT,
                show_collection_ratingkey TEXT,
                last_fingerprint TEXT,
                last_synced_at TEXT
            )
        ''')

        # Add sort_option column if missing
        cursor.execute("PRAGMA table_info(plex_collection_sync)")
        pcs_cols = [row[1] for row in cursor.fetchall()]
        if 'sort_option' not in pcs_cols:
            conn.execute('ALTER TABLE plex_collection_sync ADD COLUMN sort_option TEXT DEFAULT "default"')
            logging.info("Added sort_option column to plex_collection_sync.")

        # Per-library ratingkey tracking for multi-library collection support
        conn.execute('''
            CREATE TABLE IF NOT EXISTS plex_collection_sync_libraries (
                source_id TEXT NOT NULL,
                section_key TEXT NOT NULL,
                lib_type TEXT NOT NULL,
                ratingkey TEXT,
                PRIMARY KEY (source_id, section_key, lib_type)
            )
        ''')

        # ── Recurring startup cleanup: clear scrape_results for terminal states ──
        # scrape_results is only needed while an item is being added/checked.
        # Clear on every startup if any terminal-state items still have data,
        # and VACUUM if a significant amount was freed.
        try:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_cleanup_flags'")
            if not cursor.fetchone():
                conn.execute('''
                    CREATE TABLE schema_cleanup_flags (
                        flag TEXT PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    )
                ''')
                conn.commit()
            cur_sr = conn.execute("""
                UPDATE media_items SET scrape_results = NULL
                WHERE scrape_results IS NOT NULL
                  AND scrape_results != ''
                  AND state IN ('Collected', 'Blacklisted', 'Ghostlisted', 'Unreleased')
            """)
            if cur_sr.rowcount > 0:
                conn.commit()
                logging.info(f"[Startup] Cleared scrape_results for {cur_sr.rowcount} terminal-state items — running VACUUM")
                conn.execute("VACUUM")
                logging.info("[Startup] VACUUM complete.")
        except Exception as _ce:
            logging.warning(f"[Migration] scrape_results cleanup failed: {_ce}")

        logging.info("Attempting to commit schema migrations...")
        conn.commit()
        logging.info("Schema migrations committed successfully.")
    except Exception as e:
        conn.rollback()
        logging.error(f"Unexpected error during schema migration: {str(e)}", exc_info=True)
    finally:
        conn.close()

def verify_database():
    create_tables()
    migrate_schema()
    create_torrent_tracking_table()
    create_nzb_repair_activity_table()

    # Ensure overlay_removal_queue table exists (handles post-delete without restart)
    try:
        from .symlink_verification import (
            create_overlay_removal_queue_table,
            migrate_plex_removal_database,
        )
        create_overlay_removal_queue_table()
        migrate_plex_removal_database()
    except Exception as e:
        logging.error(f"Error ensuring overlay_removal_queue table: {e}")
    
    # Add statistics indexes
    from .migrations import add_statistics_indexes, add_search_performance_indexes, add_statistics_composite_indexes, add_database_page_indexes, add_library_covering_index
    add_statistics_indexes()
    add_statistics_composite_indexes()

    # PHASE 1.3: Add search performance indexes
    add_search_performance_indexes()
    add_database_page_indexes()

    # Library page covering index for GROUP BY query performance
    add_library_covering_index()

    # Initialize overlay system tables
    try:
        from overlays.db_init import init_overlay_tables
        init_overlay_tables()
    except Exception as e:
        logging.error(f"Error initializing overlay tables: {e}")

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
                size REAL,
                imdb_aliases TEXT,
                title_aliases TEXT,
                disable_not_wanted_check BOOLEAN DEFAULT FALSE,
                physical_release_date DATE,
                plex_verified BOOLEAN DEFAULT FALSE,
                upgrading_from_version TEXT,
                no_early_release BOOLEAN DEFAULT FALSE,
                current_score REAL DEFAULT 0,
                final_check_add_timestamp TIMESTAMP,
                real_debrid_original_title TEXT,
                rescrape_original_torrent_title TEXT,
                force_priority BOOLEAN DEFAULT FALSE,
                location_basename TEXT,
                ghostlisted BOOLEAN DEFAULT FALSE,
                theatrical_release_date DATE,
                theatrical_release_date_checked BOOLEAN DEFAULT FALSE,
                delayed_upgrade_eligible BOOLEAN DEFAULT TRUE,
                plex_labels TEXT,
                content_sources TEXT,
                verification_failed BOOLEAN DEFAULT FALSE,
                verification_failure_reason TEXT,
                plex_labels_last_synced TIMESTAMP,
                debrid_folder_name TEXT,
                source_position INTEGER
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
                permanently_failed BOOLEAN DEFAULT FALSE,
                failure_reason TEXT,
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
        # Dropping old indexes if they exist with the old structure, to ensure recreation with new structure.
        # Note: This might be better handled in a dedicated migration script for existing complex deployments.
        cursor.execute('DROP INDEX IF EXISTS idx_media_items_recent_movies')
        cursor.execute('DROP INDEX IF EXISTS idx_media_items_recent_episodes')

        cursor.execute('DROP INDEX IF EXISTS idx_media_items_recently_added')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_media_items_recently_added ON media_items(type, collected_at DESC)
            WHERE
                upgraded = 0 AND
                state IN ('Collected', 'Upgrading')
        ''')
        
        cursor.execute('DROP INDEX IF EXISTS idx_media_items_upgraded_collected_at')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_media_items_upgraded_collected_at
            ON media_items(collected_at DESC)
            WHERE collected_at IS NOT NULL AND upgraded = 1
        ''')
        
        # Create index for all_blacklisted filter optimization
        cursor.execute('DROP INDEX IF EXISTS idx_media_items_all_blacklisted')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_media_items_all_blacklisted
            ON media_items(imdb_id, type, state, season_number, episode_number, ghostlisted)
            WHERE imdb_id IS NOT NULL AND imdb_id != ''
        ''')
        
        conn.commit()
    except Exception as e:
        logging.error(f"Error creating statistics summary table or its indexes: {str(e)}")
        # Removed raise to allow application to potentially continue if this is a non-critical startup issue.
        # Depending on application requirements, raising an error might be preferred.
    finally:
        if conn:
            conn.close()
