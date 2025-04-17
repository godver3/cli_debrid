import logging
import os
import time
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple
import sqlite3

from .core import get_db_connection, retry_on_db_lock

logger = logging.getLogger(__name__)

@retry_on_db_lock()
def add_symlinked_file_for_verification(media_item_id: int, full_path: str) -> bool:
    """
    Add a symlinked file to the verification queue.
    
    Args:
        media_item_id: The ID of the media item in the database
        full_path: The full path to the symlinked file
        
    Returns:
        bool: True if successfully added, False otherwise
    """
    if not os.path.exists(full_path):
        logger.warning(f"File does not exist: {full_path}")
        return False
        
    filename = os.path.basename(full_path)
    
    conn = get_db_connection()
    try:
        # Check if this file is already in the verification queue
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM symlinked_files_verification WHERE media_item_id = ? AND full_path = ?",
            (media_item_id, full_path)
        )
        existing = cursor.fetchone()
        
        if existing:
            # File already in queue, update the verification status
            cursor.execute(
                """
                UPDATE symlinked_files_verification 
                SET verified = FALSE, 
                    verification_attempts = 0,
                    last_attempt = NULL,
                    verified_at = NULL
                WHERE id = ?
                """,
                (existing[0],)
            )
            conn.commit()
            logger.info(f"Reset verification status for existing file: {filename}")
            return True
            
        # Add new file to verification queue
        cursor.execute(
            """
            INSERT INTO symlinked_files_verification 
            (media_item_id, filename, full_path, added_at) 
            VALUES (?, ?, ?, ?)
            """,
            (media_item_id, filename, full_path, datetime.now())
        )
        conn.commit()
        logger.info(f"Added file to verification queue: {filename}")
        return True
    except Exception as e:
        conn.rollback()
        logger.error(f"Error adding file to verification queue: {str(e)}")
        return False
    finally:
        conn.close()

@retry_on_db_lock()
def get_unverified_files(limit: int = 100) -> List[Dict[str, Any]]:
    """
    Get a list of unverified files for processing.
    Excludes files that have been marked as permanently failed.
    
    Args:
        limit: Maximum number of files to return
        
    Returns:
        List of unverified files with media item details
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT 
                v.id as verification_id,
                v.media_item_id,
                v.filename,
                v.full_path,
                v.added_at,
                v.verification_attempts,
                v.last_attempt,
                m.id as item_id,
                m.title,
                m.episode_title,
                m.season_number,
                m.episode_number,
                m.type
            FROM symlinked_files_verification v
            JOIN media_items m ON v.media_item_id = m.id
            WHERE v.verified = FALSE 
            AND v.permanently_failed = FALSE
            ORDER BY v.verification_attempts ASC, v.added_at ASC
            LIMIT ?
            """,
            (limit,)
        )
        
        results = []
        for row in cursor.fetchall():
            results.append({
                'verification_id': row['verification_id'],
                'media_item_id': row['media_item_id'],
                'filename': row['filename'],
                'full_path': row['full_path'],
                'added_at': row['added_at'],
                'verification_attempts': row['verification_attempts'],
                'last_attempt': row['last_attempt'],
                'item_id': row['item_id'],
                'title': row['title'],
                'episode_title': row['episode_title'],
                'season_number': row['season_number'],
                'episode_number': row['episode_number'],
                'type': row['type']
            })
        
        return results
    except Exception as e:
        logger.error(f"Error getting unverified files: {str(e)}")
        return []
    finally:
        conn.close()

@retry_on_db_lock()
def mark_file_as_verified(verification_id: int) -> bool:
    """
    Mark a file as verified in Plex.
    
    Args:
        verification_id: The ID of the verification record
        
    Returns:
        bool: True if successfully marked, False otherwise
    """
    conn = get_db_connection()
    try:
        # Get the media_item_id
        cursor = conn.cursor()
        cursor.execute(
            "SELECT media_item_id FROM symlinked_files_verification WHERE id = ?",
            (verification_id,)
        )
        result = cursor.fetchone()
        if not result:
            logger.error(f"Verification record not found: {verification_id}")
            return False
            
        media_item_id = result[0]
        
        # Update the verification record
        cursor.execute(
            """
            UPDATE symlinked_files_verification 
            SET verified = TRUE, 
                verified_at = ?,
                verification_attempts = verification_attempts + 1,
                last_attempt = ?
            WHERE id = ?
            """,
            (datetime.now(), datetime.now(), verification_id)
        )
        
        # Update the media item
        cursor.execute(
            """
            UPDATE media_items 
            SET plex_verified = TRUE
            WHERE id = ?
            """,
            (media_item_id,)
        )
        
        conn.commit()
        logger.info(f"Marked file as verified: {verification_id}")
        return True
    except Exception as e:
        conn.rollback()
        logger.error(f"Error marking file as verified: {str(e)}")
        return False
    finally:
        conn.close()

@retry_on_db_lock()
def update_verification_attempt(verification_id: int) -> bool:
    """
    Update the verification attempt count for a file.
    
    Args:
        verification_id: The ID of the verification record
        
    Returns:
        bool: True if successfully updated, False otherwise
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE symlinked_files_verification 
            SET verification_attempts = verification_attempts + 1,
                last_attempt = ?
            WHERE id = ?
            """,
            (datetime.now(), verification_id)
        )
        conn.commit()
        logger.info(f"Updated verification attempt for file: {verification_id}")
        return True
    except Exception as e:
        conn.rollback()
        logger.error(f"Error updating verification attempt: {str(e)}")
        return False
    finally:
        conn.close()

@retry_on_db_lock()
def mark_file_as_permanently_failed(verification_id: int, reason: str) -> bool:
    """
    Mark a file as permanently failed in verification and move back to Wanted state.
    
    Args:
        verification_id: The ID of the verification record
        reason: The reason for the permanent failure
        
    Returns:
        bool: True if successfully marked, False otherwise
    """
    conn = get_db_connection()
    try:
        # Get the media_item_id
        cursor = conn.cursor()
        cursor.execute(
            "SELECT media_item_id FROM symlinked_files_verification WHERE id = ?",
            (verification_id,)
        )
        result = cursor.fetchone()
        if not result:
            logger.error(f"Verification record not found: {verification_id}")
            return False
            
        media_item_id = result[0]
        
        # Update the verification record
        cursor.execute(
            """
            UPDATE symlinked_files_verification 
            SET permanently_failed = TRUE,
                failure_reason = ?,
                last_attempt = ?
            WHERE id = ?
            """,
            (reason, datetime.now(), verification_id)
        )
        
        # Update the media item - set state back to Wanted
        cursor.execute(
            """
            UPDATE media_items 
            SET plex_verified = FALSE,
                verification_failed = TRUE,
                verification_failure_reason = ?,
                state = 'Wanted',
                last_updated = ?
            WHERE id = ?
            """,
            (reason, datetime.now(), media_item_id)
        )
        
        conn.commit()
        logger.info(f"Marked file as permanently failed and moved back to Wanted state: {verification_id} - Reason: {reason}")
        return True
    except Exception as e:
        conn.rollback()
        logger.error(f"Error marking file as permanently failed: {str(e)}")
        return False
    finally:
        conn.close()

@retry_on_db_lock()
def get_verification_stats() -> Dict[str, int]:
    """
    Get statistics about the verification process.
    
    Returns:
        Dict with verification statistics
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # Get total count
        cursor.execute("SELECT COUNT(*) FROM symlinked_files_verification")
        total = cursor.fetchone()[0]
        
        # Get verified count
        cursor.execute("SELECT COUNT(*) FROM symlinked_files_verification WHERE verified = TRUE")
        verified = cursor.fetchone()[0]
        
        # Get unverified count
        cursor.execute("SELECT COUNT(*) FROM symlinked_files_verification WHERE verified = FALSE AND permanently_failed = FALSE")
        unverified = cursor.fetchone()[0]
        
        # Get permanently failed count
        cursor.execute("SELECT COUNT(*) FROM symlinked_files_verification WHERE permanently_failed = TRUE")
        permanently_failed = cursor.fetchone()[0]
        
        # Get count of files with multiple attempts
        cursor.execute("SELECT COUNT(*) FROM symlinked_files_verification WHERE verification_attempts > 1")
        multiple_attempts = cursor.fetchone()[0]
        
        return {
            'total': total,
            'verified': verified,
            'unverified': unverified,
            'permanently_failed': permanently_failed,
            'multiple_attempts': multiple_attempts,
            'percent_verified': round((verified / total) * 100, 2) if total > 0 else 0
        }
    except Exception as e:
        logger.error(f"Error getting verification stats: {str(e)}")
        return {
            'total': 0,
            'verified': 0,
            'unverified': 0,
            'permanently_failed': 0,
            'multiple_attempts': 0,
            'percent_verified': 0
        }
    finally:
        conn.close()

@retry_on_db_lock()
def cleanup_old_verifications(days: int = 30) -> int:
    """
    Clean up old verification records that have been verified.
    
    Args:
        days: Number of days to keep verified records
        
    Returns:
        int: Number of records deleted
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            DELETE FROM symlinked_files_verification
            WHERE verified = TRUE
            AND verified_at < datetime('now', '-' || ? || ' days')
            """,
            (days,)
        )
        deleted = cursor.rowcount
        conn.commit()
        logger.info(f"Cleaned up {deleted} old verification records")
        return deleted
    except Exception as e:
        conn.rollback()
        logger.error(f"Error cleaning up old verifications: {str(e)}")
        return 0
    finally:
        conn.close()

@retry_on_db_lock()
def get_recent_unverified_files(hours: int = 24, limit: int = 100) -> List[Dict[str, Any]]:
    """
    Get a list of recently added unverified files for processing.
    Excludes files that have been marked as permanently failed.
    
    Args:
        hours: Only include files added within this many hours
        limit: Maximum number of files to return
        
    Returns:
        List of recently added unverified files with media item details
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # Calculate the timestamp for files added within the specified hours
        cutoff_time = int(time.time() - (hours * 3600))
        
        cursor.execute(
            """
            SELECT 
                v.id as verification_id,
                v.media_item_id,
                v.filename,
                v.full_path,
                v.added_at,
                v.verification_attempts,
                v.last_attempt,
                m.id as item_id,
                m.title,
                m.episode_title,
                m.season_number,
                m.episode_number,
                m.type
            FROM symlinked_files_verification v
            JOIN media_items m ON v.media_item_id = m.id
            WHERE v.verified = FALSE 
            AND v.permanently_failed = FALSE 
            AND v.added_at >= ?
            ORDER BY v.verification_attempts ASC, v.added_at ASC
            LIMIT ?
            """,
            (cutoff_time, limit)
        )
        
        results = []
        for row in cursor.fetchall():
            results.append({
                'verification_id': row['verification_id'],
                'media_item_id': row['media_item_id'],
                'filename': row['filename'],
                'full_path': row['full_path'],
                'added_at': row['added_at'],
                'verification_attempts': row['verification_attempts'],
                'last_attempt': row['last_attempt'],
                'item_id': row['item_id'],
                'title': row['title'],
                'episode_title': row['episode_title'],
                'season_number': row['season_number'],
                'episode_number': row['episode_number'],
                'type': row['type']
            })
        
        return results
    except Exception as e:
        logger.error(f"Error getting recent unverified files: {str(e)}")
        return []
    finally:
        conn.close()

def migrate_verification_database() -> bool:
    """
    Add new columns to the symlinked_files_verification and media_items tables if they don't exist.
    
    Returns:
        bool: True if migration was successful, False otherwise
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # Check and add columns to symlinked_files_verification table
        cursor.execute("PRAGMA table_info(symlinked_files_verification)")
        existing_columns = [column[1] for column in cursor.fetchall()]
        
        if 'permanently_failed' not in existing_columns:
            cursor.execute("""
                ALTER TABLE symlinked_files_verification 
                ADD COLUMN permanently_failed BOOLEAN DEFAULT FALSE
            """)
            logger.info("Added permanently_failed column to symlinked_files_verification")
            
        if 'failure_reason' not in existing_columns:
            cursor.execute("""
                ALTER TABLE symlinked_files_verification 
                ADD COLUMN failure_reason TEXT
            """)
            logger.info("Added failure_reason column to symlinked_files_verification")
            
        # Check and add columns to media_items table
        cursor.execute("PRAGMA table_info(media_items)")
        existing_columns = [column[1] for column in cursor.fetchall()]
        
        if 'verification_failed' not in existing_columns:
            cursor.execute("""
                ALTER TABLE media_items 
                ADD COLUMN verification_failed BOOLEAN DEFAULT FALSE
            """)
            logger.info("Added verification_failed column to media_items")
            
        if 'verification_failure_reason' not in existing_columns:
            cursor.execute("""
                ALTER TABLE media_items 
                ADD COLUMN verification_failure_reason TEXT
            """)
            logger.info("Added verification_failure_reason column to media_items")
            
        conn.commit()
        logger.info("Database migration completed successfully")
        return True
        
    except Exception as e:
        conn.rollback()
        logger.error(f"Error during database migration: {str(e)}")
        return False
    finally:
        conn.close()

# Run migration when module is imported
migrate_verification_database()

# --- Plex Removal Verification Queue ---

@retry_on_db_lock()
def add_path_for_removal_verification(item_path: str, item_title: str, episode_title: Optional[str] = None) -> bool:
    """
    Add a file path to the Plex removal verification queue, including item titles.

    Args:
        item_path: The full path to the file that should be removed from Plex.
        item_title: The title of the movie or show.
        episode_title: The title of the episode (if applicable).

    Returns:
        bool: True if successfully added or updated, False otherwise.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # Check if this path is already in the queue
        cursor.execute(
            """SELECT id FROM plex_removal_queue 
               WHERE item_path = ?""",
            (item_path,)
        )
        existing = cursor.fetchone()

        if existing:
            existing_id = existing[0]
            # Path exists, update titles and reset status/attempts
            cursor.execute(
                """UPDATE plex_removal_queue 
                   SET item_title = ?,
                       episode_title = ?,
                       status = 'Pending', 
                       attempts = 0, 
                       last_checked_at = NULL, 
                       added_at = ?,
                       failure_reason = NULL
                   WHERE id = ?""",
                (item_title, episode_title, datetime.now(), existing_id)
            )
            conn.commit()
            logger.info(f"Reset/Updated removal verification status to Pending for: {item_path}")
            return True
        
        # Add new path to verification queue
        cursor.execute(
            """
            INSERT INTO plex_removal_queue 
            (item_path, item_title, episode_title, status, attempts, added_at) 
            VALUES (?, ?, ?, 'Pending', 0, ?)
            """,
            (item_path, item_title, episode_title, datetime.now())
        )
        conn.commit()
        logger.info(f"Added path to Plex removal verification queue: {item_path}")
        return True
    except sqlite3.Error as e:
        conn.rollback()
        logger.error(f"Database error adding path for removal verification '{item_path}': {str(e)}")
        return False
    except Exception as e:
        conn.rollback()
        logger.error(f"Unexpected error adding path for removal verification '{item_path}': {str(e)}")
        return False
    finally:
        conn.close()

@retry_on_db_lock()
def get_pending_removal_paths(limit: int = 50) -> List[Dict[str, Any]]:
    """
    Get a list of file paths pending Plex removal verification, including titles.
    Prioritizes paths with fewer attempts.

    Args:
        limit: Maximum number of paths to return.

    Returns:
        List of pending path dictionaries including item_title and episode_title.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT 
                id,
                item_path,
                item_title, 
                episode_title,
                attempts,
                added_at,
                last_checked_at
            FROM plex_removal_queue
            WHERE status = 'Pending'
            ORDER BY attempts ASC, added_at ASC
            LIMIT ?
            """,
            (limit,)
        )
        
        results = [dict(row) for row in cursor.fetchall()]
        return results
    except sqlite3.Error as e:
        logger.error(f"Database error getting pending removal paths: {str(e)}")
        return []
    except Exception as e:
        logger.error(f"Unexpected error getting pending removal paths: {str(e)}")
        return []
    finally:
        conn.close()

@retry_on_db_lock()
def update_removal_status(queue_id: int, status: str, failure_reason: Optional[str] = None) -> bool:
    """
    Update the status of a path in the removal queue.

    Args:
        queue_id: The ID of the record in the plex_removal_queue table.
        status: The new status ('Verified', 'Failed').
        failure_reason: Optional reason if status is 'Failed'.

    Returns:
        bool: True if successfully updated, False otherwise.
    """
    if status not in ['Verified', 'Failed']:
        logger.error(f"Invalid status provided: {status}")
        return False
        
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE plex_removal_queue 
            SET status = ?, 
                failure_reason = ?,
                last_checked_at = ?
            WHERE id = ? AND status = 'Pending' 
            """,
            (status, failure_reason, datetime.now(), queue_id)
        )
        updated_rows = cursor.rowcount
        conn.commit()
        
        if updated_rows > 0:
            logger.info(f"Updated Plex removal status to '{status}' for queue ID: {queue_id}")
            return True
        else:
            # Could be that the status was already changed or ID doesn't exist
            logger.warning(f"Could not update Plex removal status for queue ID {queue_id}. Might already be updated or ID is invalid.")
            return False
            
    except sqlite3.Error as e:
        conn.rollback()
        logger.error(f"Database error updating removal status for queue ID {queue_id}: {str(e)}")
        return False
    except Exception as e:
        conn.rollback()
        logger.error(f"Unexpected error updating removal status for queue ID {queue_id}: {str(e)}")
        return False
    finally:
        conn.close()

@retry_on_db_lock()
def increment_removal_attempt(queue_id: int) -> bool:
    """
    Increment the attempt count for a pending removal path.

    Args:
        queue_id: The ID of the record in the plex_removal_queue table.

    Returns:
        bool: True if successfully updated, False otherwise.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE plex_removal_queue 
            SET attempts = attempts + 1,
                last_checked_at = ?
            WHERE id = ? AND status = 'Pending'
            """,
            (datetime.now(), queue_id)
        )
        updated_rows = cursor.rowcount
        conn.commit()
        
        if updated_rows > 0:
            logger.debug(f"Incremented Plex removal attempt count for queue ID: {queue_id}")
            return True
        else:
            logger.warning(f"Could not increment attempt count for queue ID {queue_id}. Status might not be 'Pending' or ID is invalid.")
            return False
            
    except sqlite3.Error as e:
        conn.rollback()
        logger.error(f"Database error incrementing removal attempt for queue ID {queue_id}: {str(e)}")
        return False
    except Exception as e:
        conn.rollback()
        logger.error(f"Unexpected error incrementing removal attempt for queue ID {queue_id}: {str(e)}")
        return False
    finally:
        conn.close()

@retry_on_db_lock()
def cleanup_old_verified_removals(days: int = 7) -> int:
    """
    Clean up old removal records that have been verified or failed.

    Args:
        days: Number of days to keep verified/failed records.

    Returns:
        int: Number of records deleted.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # Import timedelta if not already imported
        from datetime import timedelta
        cutoff_date = datetime.now() - timedelta(days=days)
        cursor.execute(
            """
            DELETE FROM plex_removal_queue
            WHERE status IN ('Verified', 'Failed')
            AND last_checked_at < ?
            """,
            (cutoff_date,)
        )
        deleted = cursor.rowcount
        conn.commit()
        if deleted > 0:
            logger.info(f"Cleaned up {deleted} old Plex removal verification records")
        return deleted
    except sqlite3.Error as e:
        conn.rollback()
        logger.error(f"Database error cleaning up old removal verifications: {str(e)}")
        return 0
    except Exception as e:
        conn.rollback()
        logger.error(f"Unexpected error cleaning up old removal verifications: {str(e)}")
        return 0
    finally:
        conn.close()

@retry_on_db_lock()
def get_removal_stats() -> Dict[str, int]:
    """
    Get statistics about the Plex removal verification process.

    Returns:
        Dict with removal verification statistics.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        stats = {}
        cursor.execute("SELECT COUNT(*) FROM plex_removal_queue")
        stats['total'] = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM plex_removal_queue WHERE status = 'Pending'")
        stats['pending'] = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM plex_removal_queue WHERE status = 'Verified'")
        stats['verified'] = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM plex_removal_queue WHERE status = 'Failed'")
        stats['failed'] = cursor.fetchone()[0]
        
        return stats
    except sqlite3.Error as e:
        logger.error(f"Database error getting removal stats: {str(e)}")
        return {'total': 0, 'pending': 0, 'verified': 0, 'failed': 0}
    except Exception as e:
        logger.error(f"Unexpected error getting removal stats: {str(e)}")
        return {'total': 0, 'pending': 0, 'verified': 0, 'failed': 0}
    finally:
        conn.close()

def create_plex_removal_queue_table():
    """Create the plex_removal_queue table if it doesn't exist, including title columns."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS plex_removal_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_path TEXT NOT NULL UNIQUE,
            item_title TEXT NOT NULL,
            episode_title TEXT,
            status TEXT NOT NULL CHECK(status IN ('Pending', 'Verified', 'Failed')),
            attempts INTEGER NOT NULL DEFAULT 0,
            added_at TIMESTAMP NOT NULL,
            last_checked_at TIMESTAMP,
            failure_reason TEXT
        )
        """)
        # Add indexes for faster querying
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_plex_removal_status ON plex_removal_queue (status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_plex_removal_path ON plex_removal_queue (item_path)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_plex_removal_attempts ON plex_removal_queue (attempts)")
        conn.commit()
        logger.info("Ensured plex_removal_queue table exists with title columns.")
    except sqlite3.Error as e:
        conn.rollback()
        logger.error(f"Database error creating plex_removal_queue table: {str(e)}")
    except Exception as e:
        conn.rollback()
        logger.error(f"Unexpected error creating plex_removal_queue table: {str(e)}")
    finally:
        conn.close()

def migrate_plex_removal_database() -> bool:
    """
    Add new columns or make changes to the plex_removal_queue table if needed.
    Ensures UNIQUE constraint on item_path and adds title columns.
    
    Returns:
        bool: True if migration was successful or not needed, False otherwise.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # Check for title columns first
        cursor.execute("PRAGMA table_info(plex_removal_queue)")
        existing_columns = [column[1].lower() for column in cursor.fetchall()]
        
        needs_alter = False
        if 'item_title' not in existing_columns:
            cursor.execute("ALTER TABLE plex_removal_queue ADD COLUMN item_title TEXT NOT NULL DEFAULT 'Unknown'")
            logger.info("Added item_title column to plex_removal_queue")
            needs_alter = True
        if 'episode_title' not in existing_columns:
            cursor.execute("ALTER TABLE plex_removal_queue ADD COLUMN episode_title TEXT")
            logger.info("Added episode_title column to plex_removal_queue")
            needs_alter = True
            
        if needs_alter:
             # If we added item_title with a default, remove the default now
             # SQLite ALTER TABLE doesn't directly support removing default, requires recreate for that.
             # For simplicity, we'll leave the default for now. Populate logic should handle it.
             # Alternatively, trigger the table recreation logic below if schema needs more complex changes.
             conn.commit() 
             logger.info("Committed ALTER TABLE statements for title columns.")
        
        # Now check for UNIQUE constraint (requires table recreation if missing)
        cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='plex_removal_queue'")
        result = cursor.fetchone()
        
        needs_recreation = False
        if result and result['sql']:
            table_sql = result['sql'].upper() # Use uppercase for case-insensitive check
            if 'ITEM_PATH TEXT NOT NULL UNIQUE' not in table_sql and 'UNIQUE (ITEM_PATH)' not in table_sql:
                 needs_recreation = True
                 logger.info("Found plex_removal_queue table missing UNIQUE constraint on item_path. Recreating table.")
        else:
             # Table doesn't exist, create_plex_removal_queue_table will handle it
             return True # No migration needed if table doesn't exist yet

        if needs_recreation:
            cursor.execute("PRAGMA foreign_keys=off") # Disable FK constraints temporarily
            cursor.execute("BEGIN TRANSACTION")
            
            try:
                # 1. Rename old table
                cursor.execute("DROP INDEX IF EXISTS idx_plex_removal_path") # Drop index before rename
                cursor.execute("DROP INDEX IF EXISTS idx_plex_removal_status") 
                cursor.execute("DROP INDEX IF EXISTS idx_plex_removal_attempts") 
                cursor.execute("ALTER TABLE plex_removal_queue RENAME TO plex_removal_queue_old")
                logger.info("Renamed old plex_removal_queue table.")
                
                # 2. Create new table with the correct schema (using the function)
                create_plex_removal_queue_table() # This creates the new table correctly
                
                # 3. Copy data from old table to new table, handling potential duplicates and adding default titles if needed
                cursor.execute("""
                    INSERT INTO plex_removal_queue (item_path, item_title, episode_title, status, attempts, added_at, last_checked_at, failure_reason)
                    SELECT item_path, 
                           COALESCE(item_title, 'Unknown') as item_title,
                           episode_title, 
                           status, attempts, added_at, last_checked_at, failure_reason
                    FROM (
                        SELECT *,\n                               ROW_NUMBER() OVER(PARTITION BY item_path ORDER BY added_at DESC) as rn
                        FROM plex_removal_queue_old
                    )
                    WHERE rn = 1
                """)
                copied_count = cursor.rowcount
                logger.info(f"Copied {copied_count} unique records to new plex_removal_queue table.")
                
                # 4. Drop the old table
                cursor.execute("DROP TABLE plex_removal_queue_old")
                logger.info("Dropped old plex_removal_queue table.")
                
                conn.commit() # Commit the transaction
                logger.info("Successfully recreated plex_removal_queue table with title columns and UNIQUE constraint.")
                
            except Exception as migration_err:
                conn.rollback() # Rollback on error during migration steps
                logger.error(f"Error during plex_removal_queue table recreation: {migration_err}")
                # Attempt to rename back if possible
                try:
                    # Attempt to drop the potentially partially created new table first
                    cursor.execute("DROP TABLE IF EXISTS plex_removal_queue")
                    cursor.execute("ALTER TABLE plex_removal_queue_old RENAME TO plex_removal_queue")
                    # Recreate indexes on the restored table
                    cursor.execute("CREATE INDEX IF NOT EXISTS idx_plex_removal_status ON plex_removal_queue (status)")
                    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_plex_removal_path ON plex_removal_queue (item_path)")
                    cursor.execute("CREATE INDEX IF NOT EXISTS idx_plex_removal_attempts ON plex_removal_queue (attempts)")
                    conn.commit()
                    logger.info("Rolled back table rename and attempted to restore indexes.")
                except Exception as rollback_err:
                    logger.error(f"Failed to roll back table rename/restore indexes: {rollback_err}. Manual intervention might be required.")
                return False # Indicate migration failure
            finally:
                 cursor.execute("PRAGMA foreign_keys=on") # Re-enable FK constraints

        # Add future ALTER TABLE migrations here if needed...
        conn.commit() # Commit any ALTER TABLE changes if not recreated
        return True
        
    except sqlite3.Error as e:
        conn.rollback()
        logger.error(f"Database error during Plex removal queue migration: {str(e)}")
        return False
    except Exception as e:
        conn.rollback()
        logger.error(f"Unexpected error during Plex removal queue migration: {str(e)}")
        return False
    finally:
        try:
             cursor.execute("PRAGMA foreign_keys=on") # Ensure FKs are re-enabled on exit
        except: pass # Ignore errors if cursor is invalid
        if conn:
            conn.close()

# Run creation and migration for Plex removal queue on import
create_plex_removal_queue_table()
migrate_plex_removal_database()

@retry_on_db_lock()
def remove_verification_by_media_item_id(media_item_id: int) -> int:
    """
    Remove verification record(s) associated with a specific media item ID.
    This is typically used when an item is being upgraded and the old symlink/verification is removed.

    Args:
        media_item_id: The ID of the media item whose verification record(s) should be removed.

    Returns:
        int: The number of verification records deleted.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            DELETE FROM symlinked_files_verification
            WHERE media_item_id = ?
            """,
            (media_item_id,)
        )
        deleted_count = cursor.rowcount
        conn.commit()
        if deleted_count > 0:
            logger.info(f"Deleted {deleted_count} verification record(s) for media_item_id: {media_item_id}")
        else:
            logger.debug(f"No verification records found to delete for media_item_id: {media_item_id}")
        return deleted_count
    except sqlite3.Error as e:
        conn.rollback()
        logger.error(f"Database error removing verification for media_item_id {media_item_id}: {str(e)}")
        return 0
    except Exception as e:
        conn.rollback()
        logger.error(f"Unexpected error removing verification for media_item_id {media_item_id}: {str(e)}")
        return 0
    finally:
        if conn:
            conn.close()

# --- START: Test Function ---

def test_requeue_plex_removal(item_path: str, item_title: str, episode_title: Optional[str] = None):
    """
    Manually adds or resets a specific item in the Plex removal queue for testing purposes.
    Calls add_path_for_removal_verification which handles both insertion and reset.
    """
    logger.info(f"[TEST] Attempting to re-queue item for Plex removal verification:")
    logger.info(f"[TEST]   Path: {item_path}")
    logger.info(f"[TEST]   Title: {item_title}")
    logger.info(f"[TEST]   Episode: {episode_title}")

    success = add_path_for_removal_verification(
        item_path=item_path,
        item_title=item_title,
        episode_title=episode_title
    )

    if success:
        logger.info(f"[TEST] Successfully added/reset '{item_path}' in the Plex removal queue.")
    else:
        logger.error(f"[TEST] Failed to add/reset '{item_path}' in the Plex removal queue.")

# Example usage (you would call this from another script or an interactive session):
# if __name__ == "__main__":
#     # Details for the item that was failing
#     test_path = "/mnt/symlinked/TV Shows/Happy Face (2025)/Season 01/Happy Face (2025) - S01E06 - My Jesperson Girls - tt15977292 - 1080p - (Happy Face S01E06 Lorelai 1080p AMZN WEB-DL DDP5 1 Atmos H 264-RAWR).mkv"
#     test_title = "Happy Face"
#     test_episode = "My Jesperson Girls"
#
#     print("Running test requeue...")
#     test_requeue_plex_removal(test_path, test_title, test_episode)
#     print("Test finished.")

# --- END: Test Function ---
