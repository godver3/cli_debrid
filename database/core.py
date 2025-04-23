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
def retry_on_db_lock(max_attempts=5, initial_wait=0.1, backoff_factor=2):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            attempt = 0
            while attempt < max_attempts:
                try:
                    return func(*args, **kwargs)
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e) and attempt < max_attempts - 1:
                        attempt += 1
                        # Apply jitter: Add a small random delay to the backoff
                        wait_time = initial_wait * (backoff_factor ** attempt) + random.uniform(0, 0.1 * (backoff_factor ** attempt))
                        logging.warning(f"Database locked executing {func.__name__}. Retrying in {wait_time:.2f} seconds... (Attempt {attempt + 1}/{max_attempts})")
                        time.sleep(wait_time)
                    else:
                        logging.error(f"Database error in {func.__name__} not related to lock or retries exhausted: {e}", exc_info=True)
                        raise # Re-raise the exception if it's not a lock or retries are done
            # This part should ideally not be reached if the loop finishes without returning or raising
            logging.error(f"Failed to execute {func.__name__} after {max_attempts} attempts due to persistent database locks.")
            raise sqlite3.OperationalError(f"Failed to execute {func.__name__} after {max_attempts} attempts due to database locks") # Raise the specific error
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