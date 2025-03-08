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
