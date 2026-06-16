"""
Database Initialization for Overlay System

Creates necessary database tables for the overlay system.
"""

import sqlite3
import os
import logging

logger = logging.getLogger(__name__)


def init_overlay_tables(db_path: str = None):
    """
    Initialize overlay system database tables.

    Args:
        db_path: Unused — kept for API compatibility. DB access uses get_db_connection().
    """
    try:
        from database.core import get_db_connection
        conn = get_db_connection()
        cursor = conn.cursor()

        # Migration: Rename overlay_templates to overlay_layouts if it exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='overlay_templates'")
        if cursor.fetchone():
            logger.info("Migrating overlay_templates table to overlay_layouts...")

            # Drop any conflicting index/object with the name overlay_layouts
            cursor.execute("SELECT type, name FROM sqlite_master WHERE name='overlay_layouts'")
            existing = cursor.fetchone()
            if existing:
                obj_type, obj_name = existing
                logger.info(f"Found existing {obj_type} named 'overlay_layouts', dropping it first...")
                if obj_type == 'index':
                    cursor.execute("DROP INDEX IF EXISTS overlay_layouts")
                elif obj_type == 'table':
                    logger.warning("Table overlay_layouts already exists, skipping rename")
                    # Do NOT return here — fall through to create remaining tables

            cursor.execute("ALTER TABLE overlay_templates RENAME TO overlay_layouts")
            # Rename template_data column to layout_json if it exists
            cursor.execute("PRAGMA table_info(overlay_layouts)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'template_data' in columns:
                # SQLite doesn't support direct column rename, so we need to recreate the table
                cursor.execute('''
                    CREATE TABLE overlay_layouts_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        description TEXT,
                        media_type TEXT NOT NULL,
                        layout_json TEXT NOT NULL,
                        is_default BOOLEAN DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                cursor.execute('''
                    INSERT INTO overlay_layouts_new (id, name, description, media_type, layout_json, is_default, created_at, updated_at)
                    SELECT id, name,
                           COALESCE(description, ''),
                           media_type, template_data,
                           CASE WHEN is_active = 1 THEN 1 ELSE 0 END,
                           created_at, updated_at
                    FROM overlay_layouts
                ''')
                cursor.execute("DROP TABLE overlay_layouts")
                cursor.execute("ALTER TABLE overlay_layouts_new RENAME TO overlay_layouts")
                logger.info("Successfully migrated overlay_templates to overlay_layouts with renamed columns")
            conn.commit()

        # Drop old overlay_assets table if it exists (no longer used)
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='overlay_assets'")
        if cursor.fetchone():
            logger.info("Removing unused overlay_assets table...")
            cursor.execute("DROP TABLE overlay_assets")
            conn.commit()

        # Migration: rebuild media_overlay_state if it still has the old CHECK
        # constraint (status IN ('pending', 'analyzing', 'ready', 'applied',
        # 'failed', 'skipped')) that prevents writing 'removed' / 'removal_failed'.
        cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='media_overlay_state'"
        )
        _mos_row = cursor.fetchone()
        if _mos_row and 'CHECK' in (_mos_row[0] or '').upper():
            logger.info(
                "Migrating media_overlay_state: removing old CHECK constraint on status..."
            )
            # Detect which optional columns already exist in the old table
            cursor.execute("PRAGMA table_info(media_overlay_state)")
            _existing_cols = {col[1] for col in cursor.fetchall()}
            _lh = 'last_layout_hash' in _existing_cols
            _ch = 'last_content_hash' in _existing_cols
            cursor.execute('''
                CREATE TABLE media_overlay_state_new (
                    media_item_id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL,
                    reason TEXT,
                    retry_count INTEGER DEFAULT 0,
                    last_poster_hash TEXT,
                    last_metadata_hash TEXT,
                    overlay_applied_at TIMESTAMP,
                    last_retry TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_layout_hash TEXT,
                    last_content_hash TEXT,
                    FOREIGN KEY (media_item_id) REFERENCES media_items(id) ON DELETE CASCADE
                )
            ''')
            cursor.execute(f'''
                INSERT INTO media_overlay_state_new
                    (media_item_id, status, reason, retry_count,
                     last_poster_hash, last_metadata_hash, overlay_applied_at,
                     last_retry, created_at, updated_at,
                     last_layout_hash, last_content_hash)
                SELECT media_item_id, status, reason, retry_count,
                       last_poster_hash, last_metadata_hash, overlay_applied_at,
                       last_retry, created_at, updated_at,
                       {"last_layout_hash" if _lh else "NULL"},
                       {"last_content_hash" if _ch else "NULL"}
                FROM media_overlay_state
            ''')
            cursor.execute("DROP TABLE media_overlay_state")
            cursor.execute(
                "ALTER TABLE media_overlay_state_new RENAME TO media_overlay_state"
            )
            conn.commit()
            logger.info("media_overlay_state rebuilt without CHECK constraint")

        # Table: media_overlay_state
        # Tracks overlay generation status for each media item
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS media_overlay_state (
                media_item_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                reason TEXT,
                retry_count INTEGER DEFAULT 0,
                last_poster_hash TEXT,
                last_metadata_hash TEXT,
                overlay_applied_at TIMESTAMP,
                last_retry TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (media_item_id) REFERENCES media_items(id) ON DELETE CASCADE
            )
        ''')

        # Table: overlay_layouts
        # Stores layout definitions (positioning and configuration for overlay elements)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS overlay_layouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT,
                media_type TEXT NOT NULL,
                layout_json TEXT NOT NULL,
                is_default BOOLEAN DEFAULT 0,
                is_system BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Add missing columns to existing overlay_layouts table if needed
        cursor.execute("PRAGMA table_info(overlay_layouts)")
        existing_columns = [col[1] for col in cursor.fetchall()]
        if 'description' not in existing_columns:
            logger.info("Adding missing 'description' column to overlay_layouts table...")
            cursor.execute("ALTER TABLE overlay_layouts ADD COLUMN description TEXT")
            conn.commit()
        if 'is_system' not in existing_columns:
            logger.info("Adding 'is_system' column to overlay_layouts table...")
            cursor.execute("ALTER TABLE overlay_layouts ADD COLUMN is_system BOOLEAN DEFAULT 0")
            conn.commit()

        # Add last_layout_hash to media_overlay_state if missing
        cursor.execute("PRAGMA table_info(media_overlay_state)")
        pos_columns = [col[1] for col in cursor.fetchall()]
        if 'last_layout_hash' not in pos_columns:
            logger.info("Adding 'last_layout_hash' column to media_overlay_state table...")
            cursor.execute("ALTER TABLE media_overlay_state ADD COLUMN last_layout_hash TEXT")
            conn.commit()
        if 'last_content_hash' not in pos_columns:
            logger.info("Adding 'last_content_hash' column to media_overlay_state table...")
            cursor.execute("ALTER TABLE media_overlay_state ADD COLUMN last_content_hash TEXT")
            conn.commit()
        if 'last_plex_upload_hash' not in pos_columns:
            logger.info("Adding 'last_plex_upload_hash' column to media_overlay_state table...")
            cursor.execute("ALTER TABLE media_overlay_state ADD COLUMN last_plex_upload_hash TEXT")
            conn.commit()
        if 'textless_poster_used' not in pos_columns:
            logger.info("Adding 'textless_poster_used' column to media_overlay_state table...")
            cursor.execute("ALTER TABLE media_overlay_state ADD COLUMN textless_poster_used INTEGER DEFAULT 0")
            conn.commit()
        if 'plex_thumb_url' not in pos_columns:
            logger.info("Adding 'plex_thumb_url' column to media_overlay_state table...")
            cursor.execute("ALTER TABLE media_overlay_state ADD COLUMN plex_thumb_url TEXT")
            conn.commit()

        # Table: season_overlay_state
        # Tracks overlay generation status for each TV show season (keyed by media server season item ID)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS season_overlay_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                show_ms_item_id TEXT NOT NULL,
                season_ms_item_id TEXT NOT NULL UNIQUE,
                season_number INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                reason TEXT,
                retry_count INTEGER DEFAULT 0,
                last_metadata_hash TEXT,
                overlay_applied_at TIMESTAMP,
                last_retry TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Add last_layout_hash / last_content_hash to season_overlay_state if missing
        # (must run AFTER CREATE TABLE so the table always exists first)
        cursor.execute("PRAGMA table_info(season_overlay_state)")
        sos_columns = [col[1] for col in cursor.fetchall()]
        if 'last_layout_hash' not in sos_columns:
            logger.info("Adding 'last_layout_hash' column to season_overlay_state table...")
            cursor.execute("ALTER TABLE season_overlay_state ADD COLUMN last_layout_hash TEXT")
            conn.commit()
        if 'last_content_hash' not in sos_columns:
            logger.info("Adding 'last_content_hash' column to season_overlay_state table...")
            cursor.execute("ALTER TABLE season_overlay_state ADD COLUMN last_content_hash TEXT")
            conn.commit()
        if 'last_plex_upload_hash' not in sos_columns:
            logger.info("Adding 'last_plex_upload_hash' column to season_overlay_state table...")
            cursor.execute("ALTER TABLE season_overlay_state ADD COLUMN last_plex_upload_hash TEXT")
            conn.commit()

        # Migration: rename show_plex_rating_key / season_plex_rating_key columns to
        # show_ms_item_id / season_ms_item_id if the old column names still exist.
        # SQLite does not support DROP/RENAME COLUMN directly on old versions, so we
        # recreate the table when the old columns are detected.
        if 'show_plex_rating_key' in sos_columns:
            logger.info("Migrating season_overlay_state: renaming plex_rating_key columns to ms_item_id...")
            cursor.execute('''
                CREATE TABLE season_overlay_state_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    show_ms_item_id TEXT NOT NULL,
                    season_ms_item_id TEXT NOT NULL UNIQUE,
                    season_number INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    reason TEXT,
                    retry_count INTEGER DEFAULT 0,
                    last_metadata_hash TEXT,
                    overlay_applied_at TIMESTAMP,
                    last_retry TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_layout_hash TEXT,
                    last_content_hash TEXT
                )
            ''')
            cursor.execute('''
                INSERT INTO season_overlay_state_new
                    (id, show_ms_item_id, season_ms_item_id, season_number,
                     status, reason, retry_count, last_metadata_hash,
                     overlay_applied_at, last_retry, created_at, updated_at,
                     last_layout_hash, last_content_hash)
                SELECT id, show_plex_rating_key, season_plex_rating_key, season_number,
                       status, reason, retry_count, last_metadata_hash,
                       overlay_applied_at, last_retry, created_at, updated_at,
                       last_layout_hash, last_content_hash
                FROM season_overlay_state
            ''')
            cursor.execute("DROP TABLE season_overlay_state")
            cursor.execute("ALTER TABLE season_overlay_state_new RENAME TO season_overlay_state")
            conn.commit()
            logger.info("season_overlay_state migration to ms_item_id columns complete.")

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_season_overlay_show_key
            ON season_overlay_state(show_ms_item_id)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_season_overlay_status
            ON season_overlay_state(status)
        ''')

        # Clean up season_overlay_state rows that still have old Plex integer IDs.
        # Jellyfin IDs are 32-char hex UUIDs; Plex keys are short integers (LENGTH < 20).
        # These accumulate when the season backfill ran before Jellyfin sync completed.
        # ONLY run in Jellyfin mode — on Plex, integer IDs are valid and must NOT be deleted.
        from overlays.utils import is_jellyfin_mode
        if is_jellyfin_mode():
            cursor.execute('''
                DELETE FROM season_overlay_state
                WHERE LENGTH(show_ms_item_id) < 20
            ''')
            if cursor.rowcount:
                logger.info(
                    f"Cleaned up {cursor.rowcount} season_overlay_state row(s) "
                    f"with stale Plex integer IDs (will be re-registered with Jellyfin UUIDs)"
                )

        # Table: overlay_ratings_cache
        # Persists ratings fetched from MDBList/Plex/Trakt across process restarts.
        # Eliminates cold-start API burst: on restart, ratings are read from here
        # instead of re-fetching from MDBList/Trakt for every item.
        # TTL: 7 days (checked at read time; stale entries are refreshed on demand).
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS overlay_ratings_cache (
                imdb_id    TEXT PRIMARY KEY,
                ratings    TEXT NOT NULL,   -- JSON blob of rating fields
                fetched_at REAL NOT NULL    -- Unix timestamp (time.time())
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_ratings_cache_fetched
            ON overlay_ratings_cache(fetched_at)
        ''')

        # Table: overlay_sync_state
        # Key/value store for overlay sync state (e.g. last_content_check_at timestamp)
        # Also used for cumulative cleanup counters:
        #   cleanup_total_posters  — all-time count of posters restored/cleaned
        #   cleanup_total_bytes    — all-time bytes reclaimed by cleanup runs
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS overlay_sync_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Seed cumulative cleanup counters if they don't exist yet
        cursor.execute(
            "INSERT OR IGNORE INTO overlay_sync_state (key, value) VALUES ('cleanup_total_posters', '0')"
        )
        cursor.execute(
            "INSERT OR IGNORE INTO overlay_sync_state (key, value) VALUES ('cleanup_total_bytes', '0')"
        )

        # Table: poster_backups
        # Stores information about backed-up posters
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS poster_backups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                media_item_id INTEGER NOT NULL,
                backup_path TEXT NOT NULL,
                original_hash TEXT,
                backup_size INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (media_item_id) REFERENCES media_items(id) ON DELETE CASCADE
            )
        ''')

        # Table: badge_types
        # Defines each badge category (audio_codec, resolution, hdr, etc.)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS badge_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                category TEXT NOT NULL,
                metadata_fields TEXT NOT NULL,
                is_composite BOOLEAN DEFAULT 0,
                sort_order INTEGER DEFAULT 99,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Table: badge_variations
        # One row per possible value / value-combo within a badge type
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS badge_variations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                badge_type_id INTEGER NOT NULL,
                variation_key TEXT NOT NULL,
                display_name TEXT NOT NULL,
                asset_path TEXT,
                is_default BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (badge_type_id) REFERENCES badge_types(id) ON DELETE CASCADE,
                UNIQUE(badge_type_id, variation_key)
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_badge_variations_type
            ON badge_variations(badge_type_id)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_badge_types_category
            ON badge_types(category)
        ''')

        # Table: overlay_activity
        # Persistent audit log of every overlay-related action
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS overlay_activity (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type      TEXT    NOT NULL,
                triggered_by     TEXT    NOT NULL DEFAULT 'manual',
                result           TEXT    NOT NULL DEFAULT 'success',
                title            TEXT,
                stats_json       TEXT,
                duration_seconds INTEGER,
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Migration: add duration_seconds if missing from existing tables
        cursor.execute("PRAGMA table_info(overlay_activity)")
        _oa_cols = {col[1] for col in cursor.fetchall()}
        if 'duration_seconds' not in _oa_cols:
            cursor.execute("ALTER TABLE overlay_activity ADD COLUMN duration_seconds INTEGER")
            conn.commit()

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_overlay_activity_created
            ON overlay_activity(created_at)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_overlay_activity_type
            ON overlay_activity(action_type)
        ''')

        # Create indexes for better query performance
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_overlay_state_status
            ON media_overlay_state(status)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_overlay_state_updated
            ON media_overlay_state(updated_at)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_layout_media_type
            ON overlay_layouts(media_type)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_poster_backups_media
            ON poster_backups(media_item_id)
        ''')

        conn.commit()
        conn.close()

        # Seed default badge types and variations if not already done
        try:
            from overlays.badge_manager import BadgeManager
            bm = BadgeManager(None)
            bm.seed_badge_types()
        except Exception as _seed_err:
            logger.warning(f"Failed to seed badge types: {_seed_err}")

        # Seed default layouts if none exist yet
        try:
            from overlays.layout_manager import LayoutManager
            lm = LayoutManager()
            result = lm.load_default_layouts(skip_existing=True)
            if result['loaded']:
                logger.info(f"Seeded {result['loaded']} default layout(s)")
        except Exception as _layout_err:
            logger.warning(f"Failed to seed default layouts: {_layout_err}")

        # Restore user layouts from filesystem backup if DB was just wiped.
        # User-created layouts are saved to /user/config/overlay_layouts/ on every
        # create/update. On a fresh DB the table is empty — restore those files now.
        try:
            from overlays.layout_manager import LayoutManager
            lm = LayoutManager()
            restore_result = lm.restore_from_filesystem(skip_existing=True)
            if restore_result['loaded']:
                logger.info(f"Restored {restore_result['loaded']} user layout(s) from filesystem backup.")
            if restore_result['errors']:
                logger.warning(f"Layout restore errors: {restore_result['errors']}")
        except Exception as _restore_err:
            logger.warning(f"Failed to restore layouts from filesystem: {_restore_err}")

        logger.info("Overlay database tables initialized successfully")
        return True

    except Exception as e:
        logger.error(f"Failed to initialize overlay tables: {e}", exc_info=True)
        return False


if __name__ == '__main__':
    # Allow running this script directly for manual initialization
    logging.basicConfig(level=logging.INFO)
    success = init_overlay_tables()
    if success:
        print("✓ Overlay database tables created successfully")
    else:
        print("✗ Failed to create overlay database tables")
