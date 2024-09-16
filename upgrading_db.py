import sqlite3
from datetime import datetime, timedelta
import logging

def get_upgrading_db_connection():
    conn = sqlite3.connect('user/db_content/upgrading_items.db')
    conn.row_factory = sqlite3.Row
    return conn

def create_upgrading_table():
    conn = get_upgrading_db_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS upgrading_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_id INTEGER,
            imdb_id TEXT,
            tmdb_id TEXT,
            title TEXT,
            year INTEGER,
            type TEXT,
            season_number INTEGER,
            episode_number INTEGER,
            filled_by_title TEXT,
            filled_by_magnet TEXT,
            last_checked TIMESTAMP,
            check_count INTEGER DEFAULT 0,
            UNIQUE(imdb_id, tmdb_id, title, year, season_number, episode_number)
        )
    ''')
    conn.commit()
    conn.close()

def add_to_upgrading(item):
    conn = get_upgrading_db_connection()
    try:
        conn.execute('''
            INSERT OR REPLACE INTO upgrading_items
            (original_id, imdb_id, tmdb_id, title, year, type, season_number, episode_number, filled_by_title, filled_by_magnet, last_checked, check_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        ''', (
            item['id'], item['imdb_id'], item['tmdb_id'], item['title'], item['year'],
            item['type'], item.get('season_number'), item.get('episode_number'),
            item['filled_by_title'], item['filled_by_magnet'], datetime.now()
        ))
        conn.commit()
        logging.debug(f"Added item to Upgrading database: {item['title']}")
    except Exception as e:
        logging.error(f"Error adding item to Upgrading database: {str(e)}")
    finally:
        conn.close()

def remove_from_upgrading(item_id):
    conn = get_upgrading_db_connection()
    try:
        conn.execute('DELETE FROM upgrading_items WHERE original_id = ?', (item_id,))
        conn.commit()
        logging.debug(f"Removed item from Upgrading database: ID {item_id}")
    except Exception as e:
        logging.error(f"Error removing item from Upgrading database: {str(e)}")
    finally:
        conn.close()

def get_items_to_check():
    conn = get_upgrading_db_connection()
    try:
        three_days_ago = datetime.now() - timedelta(days=3)
        six_hours_ago = datetime.now() - timedelta(hours=6)
        cursor = conn.execute('''
            SELECT * FROM upgrading_items
            WHERE (last_checked IS NULL OR last_checked < ?)
            AND (check_count < 12)
            AND (last_checked IS NULL OR last_checked < ?)
        ''', (six_hours_ago, three_days_ago))
        items = cursor.fetchall()
        return [dict(item) for item in items]
    except Exception as e:
        logging.error(f"Error retrieving items to check from Upgrading database: {str(e)}")
        return []
    finally:
        conn.close()

def update_check_count(item_id):
    conn = get_upgrading_db_connection()
    try:
        conn.execute('''
            UPDATE upgrading_items
            SET check_count = check_count + 1, last_checked = ?
            WHERE original_id = ?
        ''', (datetime.now(), item_id))
        conn.commit()
        logging.debug(f"Updated check count for item ID {item_id} in Upgrading database")
    except Exception as e:
        logging.error(f"Error updating check count in Upgrading database: {str(e)}")
    finally:
        conn.close()