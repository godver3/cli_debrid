"""Content Source Removal Retry Queue

Handles retrying content source removal operations that fail or timeout during deletion.
When a show is deleted but content source removal times out (e.g., Trakt API cooldown),
the removal is queued for retry to prevent the show from being re-added.
"""

import logging
from database.core import get_db_connection
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
import json


def create_retry_queue_table():
    """Create the content_source_removal_retry table if it doesn't exist"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS content_source_removal_retry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                imdb_id TEXT NOT NULL,
                title TEXT,
                type TEXT NOT NULL,
                item_data TEXT,
                retry_count INTEGER DEFAULT 0,
                max_retries INTEGER DEFAULT 5,
                next_retry_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_attempt_at TIMESTAMP,
                last_error TEXT,
                completed BOOLEAN DEFAULT FALSE,
                completed_at TIMESTAMP
            )
        ''')

        # Create index for efficient querying
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_retry_queue_next_retry
            ON content_source_removal_retry(completed, next_retry_at)
        ''')

        conn.commit()
        logging.info("[RETRY_QUEUE] Created content_source_removal_retry table")
    except Exception as e:
        logging.error(f"[RETRY_QUEUE] Failed to create retry queue table: {e}")
    finally:
        conn.close()


def add_to_retry_queue(imdb_id: str, title: str, item_type: str, item_data: Dict[str, Any],
                       error: Optional[str] = None) -> bool:
    """
    Add a content source removal to the retry queue

    Args:
        imdb_id: IMDB ID of the show
        title: Title of the show
        item_type: Type ('show')
        item_data: Full item data dict for removal
        error: Optional error message from failed attempt

    Returns:
        True if successfully added, False otherwise
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Check if item already in queue
        cursor.execute(
            'SELECT id, retry_count FROM content_source_removal_retry WHERE imdb_id = ? AND completed = FALSE',
            (imdb_id,)
        )
        existing = cursor.fetchone()

        if existing:
            # Update existing entry
            retry_id, retry_count = existing
            next_retry = datetime.now() + timedelta(minutes=2 ** min(retry_count, 5))  # Exponential backoff

            cursor.execute('''
                UPDATE content_source_removal_retry
                SET retry_count = retry_count + 1,
                    next_retry_at = ?,
                    last_attempt_at = ?,
                    last_error = ?,
                    item_data = ?
                WHERE id = ?
            ''', (next_retry, datetime.now(), error, json.dumps(item_data), retry_id))

            logging.info(f"[RETRY_QUEUE] Updated retry entry for {title} ({imdb_id}) - attempt {retry_count + 1}, next retry at {next_retry}")
        else:
            # Add new entry with first retry in 2 minutes
            next_retry = datetime.now() + timedelta(minutes=2)

            cursor.execute('''
                INSERT INTO content_source_removal_retry
                (imdb_id, title, type, item_data, next_retry_at, last_attempt_at, last_error)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (imdb_id, title, item_type, json.dumps(item_data), next_retry, datetime.now(), error))

            logging.info(f"[RETRY_QUEUE] Added {title} ({imdb_id}) to retry queue - first retry at {next_retry}")

        conn.commit()
        conn.close()
        return True

    except Exception as e:
        logging.error(f"[RETRY_QUEUE] Failed to add to retry queue: {e}")
        return False


def get_retry_queue_items(limit: int = 10) -> List[Dict[str, Any]]:
    """
    Get items ready for retry

    Args:
        limit: Maximum number of items to return

    Returns:
        List of retry queue items ready for processing
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT id, imdb_id, title, type, item_data, retry_count, max_retries, last_error
            FROM content_source_removal_retry
            WHERE completed = FALSE
              AND retry_count < max_retries
              AND (next_retry_at IS NULL OR next_retry_at <= ?)
            ORDER BY next_retry_at ASC
            LIMIT ?
        ''', (datetime.now(), limit))

        rows = cursor.fetchall()
        conn.close()

        items = []
        for row in rows:
            items.append({
                'id': row[0],
                'imdb_id': row[1],
                'title': row[2],
                'type': row[3],
                'item_data': json.loads(row[4]) if row[4] else {},
                'retry_count': row[5],
                'max_retries': row[6],
                'last_error': row[7]
            })

        return items

    except Exception as e:
        logging.error(f"[RETRY_QUEUE] Failed to get retry queue items: {e}")
        return []


def mark_retry_completed(retry_id: int, success: bool = True, error: Optional[str] = None):
    """
    Mark a retry item as completed or failed

    Args:
        retry_id: ID of the retry queue item
        success: Whether the retry succeeded
        error: Optional error message if failed
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        if success:
            cursor.execute('''
                UPDATE content_source_removal_retry
                SET completed = TRUE,
                    completed_at = ?,
                    last_attempt_at = ?
                WHERE id = ?
            ''', (datetime.now(), datetime.now(), retry_id))
            logging.info(f"[RETRY_QUEUE] Marked retry {retry_id} as completed")
        else:
            # Failed, update retry count and next retry time
            cursor.execute('''
                SELECT retry_count, max_retries FROM content_source_removal_retry WHERE id = ?
            ''', (retry_id,))
            row = cursor.fetchone()

            if row:
                retry_count, max_retries = row

                if retry_count >= max_retries:
                    # Exceeded max retries, mark as completed (failed)
                    cursor.execute('''
                        UPDATE content_source_removal_retry
                        SET completed = TRUE,
                            completed_at = ?,
                            last_attempt_at = ?,
                            last_error = ?
                        WHERE id = ?
                    ''', (datetime.now(), datetime.now(), f"Max retries exceeded: {error}", retry_id))
                    logging.warning(f"[RETRY_QUEUE] Retry {retry_id} exceeded max retries ({max_retries})")
                else:
                    # Schedule next retry with exponential backoff
                    next_retry = datetime.now() + timedelta(minutes=2 ** min(retry_count, 5))
                    cursor.execute('''
                        UPDATE content_source_removal_retry
                        SET retry_count = retry_count + 1,
                            next_retry_at = ?,
                            last_attempt_at = ?,
                            last_error = ?
                        WHERE id = ?
                    ''', (next_retry, datetime.now(), error, retry_id))
                    logging.info(f"[RETRY_QUEUE] Scheduled retry {retry_id} for {next_retry} (attempt {retry_count + 1}/{max_retries})")

        conn.commit()
        conn.close()

    except Exception as e:
        logging.error(f"[RETRY_QUEUE] Failed to mark retry completed: {e}")


def get_retry_queue_stats() -> Dict[str, Any]:
    """Get statistics about the retry queue"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN completed = FALSE THEN 1 ELSE 0 END) as pending,
                SUM(CASE WHEN completed = TRUE THEN 1 ELSE 0 END) as completed,
                SUM(CASE WHEN retry_count >= max_retries AND completed = FALSE THEN 1 ELSE 0 END) as failed
            FROM content_source_removal_retry
        ''')

        row = cursor.fetchone()
        conn.close()

        return {
            'total': row[0] or 0,
            'pending': row[1] or 0,
            'completed': row[2] or 0,
            'failed': row[3] or 0
        }

    except Exception as e:
        logging.error(f"[RETRY_QUEUE] Failed to get stats: {e}")
        return {'total': 0, 'pending': 0, 'completed': 0, 'failed': 0}


def cleanup_old_completed_retries(days: int = 7):
    """Remove completed retry entries older than specified days"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cutoff = datetime.now() - timedelta(days=days)
        cursor.execute('''
            DELETE FROM content_source_removal_retry
            WHERE completed = TRUE AND completed_at < ?
        ''', (cutoff,))

        deleted = cursor.rowcount
        conn.commit()
        conn.close()

        if deleted > 0:
            logging.info(f"[RETRY_QUEUE] Cleaned up {deleted} old completed retry entries")

        return deleted

    except Exception as e:
        logging.error(f"[RETRY_QUEUE] Failed to cleanup old retries: {e}")
        return 0
