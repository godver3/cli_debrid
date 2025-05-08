import os
import sqlite3
import unicodedata
from typing import Any, Dict, List, Optional, Tuple
from sqlite3 import Row
from functools import wraps
import logging
import time
import random
import uuid
from datetime import datetime

# --- Constants ---
MAX_STORED_NOTIFICATIONS = 50 # Define max notifications to keep in DB
DEFAULT_LONG_EXECUTION_THRESHOLD_SECONDS = 1.0 # Define a default threshold

# --- String Normalization ---
def normalize_string(input_str):
    return ''.join(
        c for c in unicodedata.normalize('NFKD', input_str)
        if unicodedata.category(c) != 'Mn'
    )

# --- Row Conversion ---
def row_to_dict(row: Row) -> Dict[str, Any]:
    return {key: row[key] for key in row.keys()}

# --- Retry Decorator --- Moved UP ---
def retry_on_db_lock(max_attempts=5, initial_wait=0.1, backoff_factor=2,
                     long_execution_threshold_seconds=DEFAULT_LONG_EXECUTION_THRESHOLD_SECONDS):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            overall_start_time = time.monotonic()
            attempt = 0 # Number of failed attempts so far
            last_exception = None

            while attempt < max_attempts:
                try:
                    result = func(*args, **kwargs)
                    # Successful execution
                    overall_end_time = time.monotonic()
                    duration = overall_end_time - overall_start_time
                    if duration > long_execution_threshold_seconds:
                        logging.warning(
                            f"Function {func.__name__} executed successfully but took {duration:.3f}s "
                            f"(threshold: {long_execution_threshold_seconds:.1f}s). "
                            f"This may indicate a long-running transaction or operation."
                        )
                    return result
                except sqlite3.OperationalError as e:
                    last_exception = e
                    if "database is locked" in str(e):
                        # Check if we have retries left.
                        # attempt is 0-indexed count of failures. So, if attempt = max_attempts - 1, all retries are exhausted.
                        if attempt < max_attempts - 1:
                            current_failed_attempt_count = attempt + 1
                            # For the 1st retry (current_failed_attempt_count=1), power is 1.
                            base_wait = initial_wait * (backoff_factor ** current_failed_attempt_count)
                            jitter = random.uniform(0, 0.1 * base_wait)
                            actual_wait_time = base_wait + jitter
                            
                            logging.warning(
                                f"Database locked executing {func.__name__} (attempt {current_failed_attempt_count} of {max_attempts -1} retries). "
                                f"Retrying in {actual_wait_time:.3f}s..."
                            )
                            time.sleep(actual_wait_time)
                            attempt += 1 # Increment after sleep, before next try
                        else:
                            # All retries used up for "database is locked"
                            attempt += 1 # Reflect this last failed attempt
                            break # Exit loop to handle final failure
                    else:
                        # Database error not related to lock
                        overall_end_time = time.monotonic()
                        duration = overall_end_time - overall_start_time
                        logging.error(
                            f"Database error in {func.__name__} (not a lock): {e}. "
                            f"Total execution time before this error: {duration:.3f}s.",
                            exc_info=True
                        )
                        raise # Re-raise this specific non-lock operational error
                except Exception as e: # Catch any other unexpected exception from func
                    overall_end_time = time.monotonic()
                    duration = overall_end_time - overall_start_time
                    logging.error(
                        f"Unexpected error in {func.__name__}: {e}. "
                        f"Total execution time before this error: {duration:.3f}s.",
                        exc_info=True
                    )
                    raise # Re-raise unexpected error

            # If loop finishes, it means all attempts failed (most likely due to DB lock if last_exception is set)
            overall_end_time = time.monotonic()
            duration = overall_end_time - overall_start_time
            
            if last_exception and "database is locked" in str(last_exception):
                final_message = (
                    f"Failed to execute {func.__name__} after {max_attempts} attempts ({duration:.3f}s total) "
                    f"due to persistent database locks. Last error: {last_exception}"
                )
                logging.error(final_message)
                raise last_exception # Re-raise the last "database is locked" error
            elif last_exception: # Should have been handled by raises inside the loop
                # This case should ideally not be reached if non-lock errors raise immediately
                logging.error(f"Failed to execute {func.__name__} after {duration:.3f}s with unhandled error: {last_exception}", exc_info=True)
                raise last_exception
            else:
                # Should not happen if func always runs or raises
                fallback_message = f"Failed to execute {func.__name__} after {max_attempts} attempts ({duration:.3f}s total), reason unclear."
                logging.error(fallback_message)
                raise sqlite3.OperationalError(fallback_message)
        return wrapper
    return decorator

# --- Schema Initialization --- Now defined AFTER the decorator ---
@retry_on_db_lock()
def initialize_notifications_table(conn: sqlite3.Connection):
    """Creates the notifications table if it doesn't exist."""
    try:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS notifications (
                id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                type TEXT NOT NULL,
                link TEXT,
                read INTEGER NOT NULL DEFAULT 0 CHECK(read IN (0, 1))
            )
        ''')
        # Optional: Add index for faster querying/sorting by time
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_notifications_timestamp ON notifications (timestamp)
        ''')
        # Optional: Add index for faster querying by read status
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_notifications_read ON notifications (read)
        ''')
        conn.commit()
        # logging.debug("Notifications table initialized successfully.")
    except sqlite3.Error as e:
        logging.error(f"Database error initializing notifications table: {e}")
        conn.rollback()
        # Do not close connection here, let the caller manage it

# --- Database Connection --- Now defined AFTER initialize_notifications_table ---
def get_db_connection(db_path=None):
    if db_path is None:
        # Get db_content directory from environment variable with fallback
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        db_path = os.path.join(db_content_dir, 'media_items.db')
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)  # Increased timeout slightly
    conn.execute('PRAGMA journal_mode=WAL')  # Enable WAL mode
    conn.row_factory = sqlite3.Row
    
    # REMOVED: Initialization moved to schema_management.py
    # if is_new_db:
    #     logging.info(f"New database created at {db_path}. Ensure schema is initialized/migrated.")
    #     # Initialize other tables if needed here via schema management
    # else:
    #     # Ensure notifications table exists - handled by migration now
    #     pass

    return conn

# --- Media Item Specific Functions (Example) ---
def get_existing_airtime(conn, imdb_id):
    cursor = conn.execute('''
        SELECT airtime FROM media_items
        WHERE imdb_id = ? AND type = 'episode' AND airtime IS NOT NULL
        LIMIT 1
    ''', (imdb_id,))
    result = cursor.fetchone()
    return result[0] if result else None

@retry_on_db_lock()
def reset_item_to_upgrading(imdb_id: str, original_file: str, original_version: str):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE media_items
            SET state = 'Upgrading',
                filled_by_file = ?,
                filled_by_title = ?,
                version = ?,
                upgrading_from = NULL
            WHERE imdb_id = ?
        ''', (original_file, original_file, original_version, imdb_id))
        conn.commit()
        logging.info(f"Reset item with IMDB ID {imdb_id} to Upgrading state with original file: {original_file}")
    except sqlite3.Error as e:
        logging.error(f"Database error: {e}")
        conn.rollback()
    finally:
        conn.close()

# --- Notification Functions ---

@retry_on_db_lock()
def add_db_notification(
    title: str,
    message: str,
    notification_type: str = 'info',
    link: Optional[str] = None,
    is_read: bool = False
) -> Tuple[bool, Optional[str]]:
    """Adds a notification to the database and prunes old ones."""
    conn = get_db_connection()
    notification_id = str(uuid.uuid4())
    timestamp = datetime.now().isoformat()
    read_status = 1 if is_read else 0
    
    try:
        cursor = conn.cursor()
        # Insert new notification
        cursor.execute('''
            INSERT INTO notifications (id, timestamp, title, message, type, link, read)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (notification_id, timestamp, title, message, notification_type, link, read_status))
        
        # Prune old notifications (keep MAX_STORED_NOTIFICATIONS newest)
        cursor.execute(f'''
            DELETE FROM notifications
            WHERE id NOT IN (
                SELECT id FROM notifications ORDER BY timestamp DESC LIMIT ?
            )
        ''', (MAX_STORED_NOTIFICATIONS,))
        
        conn.commit()
        # logging.debug(f"Added notification {notification_id} and pruned old ones.")
        return True, None # Success
    except sqlite3.Error as e:
        logging.error(f"Database error adding notification: {e}")
        conn.rollback()
        return False, str(e) # Failure + error message
    finally:
        conn.close()

@retry_on_db_lock()
def get_db_notifications(
    limit: Optional[int] = None, 
    sort_order: str = 'DESC'
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Gets notifications from the database, ordered by timestamp."""
    conn = get_db_connection()
    notifications = []
    query = f"SELECT * FROM notifications ORDER BY timestamp {sort_order}"
    params = []
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
        
    try:
        cursor = conn.cursor()
        cursor.execute(query, params)
        rows = cursor.fetchall()
        notifications = [row_to_dict(row) for row in rows]
        return notifications, None # Success
    except sqlite3.Error as e:
        logging.error(f"Database error getting notifications: {e}")
        return [], str(e) # Failure + error message
    finally:
        conn.close()

@retry_on_db_lock()
def mark_db_notification_read(notification_id: str) -> Tuple[bool, bool, Optional[str]]:
    """Marks a single notification as read. Returns (success, found, error_message)."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE notifications SET read = 1 WHERE id = ?", (notification_id,))
        conn.commit()
        
        # Check if any row was actually updated
        found = cursor.rowcount > 0
        if found:
            # logging.debug(f"Marked notification {notification_id} as read.")
            pass
        else:
            logging.warning(f"Attempted to mark notification as read, but ID not found: {notification_id}")
            
        return True, found, None # Success, Found status, No error
    except sqlite3.Error as e:
        logging.error(f"Database error marking notification {notification_id} as read: {e}")
        conn.rollback()
        return False, False, str(e) # Failure, Not Found, Error message
    finally:
        conn.close()

@retry_on_db_lock()
def mark_all_db_notifications_read() -> Tuple[bool, int, Optional[str]]:
    """Marks all notifications as read. Returns (success, count_updated, error_message)."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # Update only those that are currently unread
        cursor.execute("UPDATE notifications SET read = 1 WHERE read = 0")
        count_updated = cursor.rowcount
        conn.commit()
        # logging.debug(f"Marked {count_updated} notifications as read.")
        return True, count_updated, None # Success, Count updated, No error
    except sqlite3.Error as e:
        logging.error(f"Database error marking all notifications as read: {e}")
        conn.rollback()
        return False, 0, str(e) # Failure, 0 updated, Error message
    finally:
        conn.close()