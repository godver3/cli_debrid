import os
import sqlite3
import unicodedata
from typing import Any, Dict
from sqlite3 import Row
from functools import wraps
import logging
import time
import random

def get_db_connection():
    db_path = os.path.join('/user/db_content', 'media_items.db')
    conn = sqlite3.connect(db_path)
    conn.execute('PRAGMA journal_mode=WAL')  # Enable WAL mode
    conn.row_factory = sqlite3.Row
    return conn

def normalize_string(input_str):
    return ''.join(
        c for c in unicodedata.normalize('NFKD', input_str)
        if unicodedata.category(c) != 'Mn'
    )

def row_to_dict(row: Row) -> Dict[str, Any]:
    return {key: row[key] for key in row.keys()}

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
                        wait_time = initial_wait * (backoff_factor ** attempt) + random.uniform(0, 0.1)
                        logging.warning(f"Database locked. Retrying in {wait_time:.2f} seconds... (Attempt {attempt + 1}/{max_attempts})")
                        time.sleep(wait_time)
                    else:
                        raise
            raise Exception(f"Failed to execute {func.__name__} after {max_attempts} attempts due to database locks")
        return wrapper
    return decorator

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