import sqlite3
from datetime import datetime

def get_db_connection():
    conn = sqlite3.connect('content_verification.db')
    conn.row_factory = sqlite3.Row
    return conn

def create_cache_table():
    conn = get_db_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS cache_release_dates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imdb_id TEXT,
            media_type TEXT,
            season INTEGER,
            episode INTEGER,
            release_date DATE,
            last_checked TIMESTAMP,
            UNIQUE(imdb_id, media_type, season, episode)
        )
    ''')
    conn.commit()
    conn.close()

def get_cached_release_date(imdb_id, media_type, season=None, episode=None):
    conn = get_db_connection()
    cursor = conn.execute('''
        SELECT release_date, last_checked FROM cache_release_dates
        WHERE imdb_id = ? AND media_type = ? AND (season IS NULL OR season = ?) AND (episode IS NULL OR episode = ?)
    ''', (imdb_id, media_type, season, episode))
    result = cursor.fetchone()
    conn.close()
    return result

def update_cache_release_date(imdb_id, media_type, release_date, season=None, episode=None):
    conn = get_db_connection()
    conn.execute('''
        INSERT OR REPLACE INTO cache_release_dates
        (imdb_id, media_type, season, episode, release_date, last_checked)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (imdb_id, media_type, season, episode, release_date, datetime.now()))
    conn.commit()
    conn.close()
