import sqlite3
import logging
from datetime import datetime
import unicodedata
import os
from logging_config import get_logger

logger = get_logger()

def get_db_connection():
    conn = sqlite3.connect('db_content/media_items.db')
    conn.row_factory = sqlite3.Row
    return conn

def normalize_string(input_str):
    return ''.join(
        c for c in unicodedata.normalize('NFKD', input_str)
        if unicodedata.category(c) != 'Mn'
    )

def create_tables():
    conn = get_db_connection()
    conn.execute('''
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
            filled_by_title TEXT,
            filled_by_magnet TEXT,
            last_updated TIMESTAMP,
            UNIQUE(imdb_id, tmdb_id, title, year, season_number, episode_number)
        )
    ''')
    conn.commit()
    conn.close()

def create_database():
    create_tables()
    logger.info("Database created and tables initialized.")

def add_collected_items(media_items_batch):
    conn = get_db_connection()
    try:
        processed_items = set()
        for item in media_items_batch:
            normalized_title = normalize_string(item.get('title', 'Unknown'))
            if 'season_number' in item and 'episode_number' in item:
                # It's an episode
                existing_episode = conn.execute('''
                    SELECT id FROM media_items
                    WHERE imdb_id = ? AND season_number = ? AND episode_number = ? AND type = "episode"
                ''', (item['imdb_id'], item['season_number'], item['episode_number'])).fetchone()
                
                if not existing_episode:
                    conn.execute('''
                        INSERT INTO media_items
                        (imdb_id, tmdb_id, title, year, release_date, state, type, season_number, episode_number, episode_title, last_updated)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        item['imdb_id'], item['tmdb_id'], normalized_title, item['year'], item.get('release_date', None), 'Collected', 'episode',
                        item['season_number'], item['episode_number'], item.get('episode_title', ''), datetime.now()
                    ))
                    logger.debug(f"Adding episode to DB: {normalized_title} S{item['season_number']}E{item['episode_number']}")
                else:
                    conn.execute('''
                        UPDATE media_items
                        SET tmdb_id = ?, title = ?, year = ?, release_date = ?, state = ?, type = ?, episode_title = ?, last_updated = ?
                        WHERE imdb_id = ? AND season_number = ? AND episode_number = ? AND type = "episode"
                    ''', (item['tmdb_id'], normalized_title, item['year'], item.get('release_date', None), 'Collected', 'episode',
                        item.get('episode_title', ''), datetime.now(), item['imdb_id'], item['season_number'], item['episode_number']))
                    logger.debug(f"Updating episode in DB: {normalized_title} S{item['season_number']}E{item['episode_number']}")
                processed_items.add((item['imdb_id'], item['season_number'], item['episode_number']))

            else:
                # It's a movie
                existing_movie = conn.execute('SELECT id FROM media_items WHERE imdb_id = ? AND type = "movie"', (item['imdb_id'],)).fetchone()
                if not existing_movie:
                    conn.execute('''
                        INSERT INTO media_items
                        (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (item['imdb_id'], item['tmdb_id'], normalized_title, item['year'], item.get('release_date', None), 'Collected', 'movie', datetime.now()))
                    logger.debug(f"Adding movie to DB: {normalized_title}")
                else:
                    conn.execute('''
                        UPDATE media_items
                        SET tmdb_id = ?, title = ?, year = ?, release_date = ?, state = ?, type = ?, last_updated = ?
                        WHERE imdb_id = ? AND type = "movie"
                    ''', (item['tmdb_id'], normalized_title, item['year'], item.get('release_date', None), 'Collected', 'movie', datetime.now(), item['imdb_id']))
                    logger.debug(f"Updating movie in DB: {normalized_title}")
                processed_items.add(item['imdb_id'])

        # Remove outdated collected items
        collected_items = conn.execute('SELECT id, imdb_id, season_number, episode_number FROM media_items WHERE state = "Collected"').fetchall()
        for collected_item in collected_items:
            if collected_item['season_number'] is not None and collected_item['episode_number'] is not None:
                item_tuple = (collected_item['imdb_id'], collected_item['season_number'], collected_item['episode_number'])
            else:
                item_tuple = collected_item['imdb_id']

            if item_tuple not in processed_items:
                conn.execute('DELETE FROM media_items WHERE id = ?', (collected_item['id'],))
                logger.debug(f"Removed outdated collected item from DB: {collected_item['imdb_id']}")

        conn.commit()
        logger.info("Collected content scanned and database updated.")
    except Exception as e:
        logger.error(f"Error scanning collected content: {str(e)}")
    finally:
        conn.close()

def add_wanted_items(media_items_batch):
    conn = get_db_connection()
    try:
        for item in media_items_batch:
            normalized_title = normalize_string(item.get('title', 'Unknown'))
            if 'season_number' in item and 'episode_number' in item:
                # It's an episode
                existing_episode = conn.execute('''
                    SELECT state FROM media_items
                    WHERE imdb_id = ? AND season_number = ? AND episode_number = ?
                ''', (item['imdb_id'], item['season_number'], item['episode_number'])).fetchone()

                if not existing_episode:
                    # Insert new episode as Wanted
                    conn.execute('''
                        INSERT INTO media_items
                        (imdb_id, tmdb_id, title, year, release_date, state, type, season_number, episode_number, episode_title, last_updated)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        item['imdb_id'], item['tmdb_id'], normalized_title, item['year'], item.get('release_date', 'Unknown'), 'Wanted', 'episode',
                        item['season_number'], item['episode_number'], item.get('episode_title', ''), datetime.now()
                    ))
                    logger.debug(f"Adding new episode as Wanted in DB: {normalized_title} S{item['season_number']}E{item['episode_number']}")
                elif existing_episode['state'] != 'Collected':
                    # Update existing episode to Wanted if not already Collected
                    conn.execute('''
                        UPDATE media_items
                        SET tmdb_id = ?, title = ?, year = ?, release_date = ?, state = ?, type = ?, season_number = ?, episode_number = ?, episode_title = ?, last_updated = ?
                        WHERE imdb_id = ? AND season_number = ? AND episode_number = ?
                    ''', (
                        item['tmdb_id'], normalized_title, item['year'], item.get('release_date', 'Unknown'), 'Wanted', 'episode',
                        item['season_number'], item['episode_number'], item.get('episode_title', ''), datetime.now(), item['imdb_id'], item['season_number'], item['episode_number']
                    ))
                    logger.debug(f"Updating episode to Wanted in DB: {normalized_title} S{item['season_number']}E{item['episode_number']}")

            else:
                # It's a movie
                existing_movie = conn.execute('''
                    SELECT state FROM media_items
                    WHERE imdb_id = ?
                ''', (item['imdb_id'],)).fetchone()

                if not existing_movie:
                    # Insert new movie as Wanted
                    conn.execute('''
                        INSERT INTO media_items
                        (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (item['imdb_id'], item['tmdb_id'], normalized_title, item['year'], item.get('release_date', 'Unknown'), 'Wanted', 'movie', datetime.now()))
                    logger.debug(f"Adding new movie as Wanted in DB: {normalized_title}")
                elif existing_movie['state'] != 'Collected':
                    # Update existing movie to Wanted if not already Collected
                    conn.execute('''
                        UPDATE media_items
                        SET tmdb_id = ?, title = ?, year = ?, release_date = ?, state = ?, type = ?, last_updated = ?
                        WHERE imdb_id = ?
                    ''', (item['tmdb_id'], normalized_title, item['year'], item.get('release_date', 'Unknown'), 'Wanted', 'movie', datetime.now(), item['imdb_id']))
                    logger.debug(f"Updating movie to Wanted in DB: {normalized_title}")

        conn.commit()
        logger.info("Wanted items processed and database updated.")
    except Exception as e:
        logger.error(f"Error adding wanted items: {str(e)}")
    finally:
        conn.close()

def get_media_item_status(imdb_id=None, tmdb_id=None, title=None, year=None, season_number=None, episode_number=None):
    conn = get_db_connection()
    try:
        if season_number is not None and episode_number is not None:
            # Check for TV show episode
            query = '''
                SELECT state FROM media_items
                WHERE imdb_id = ? AND season_number = ? AND episode_number = ?
            '''
            params = (imdb_id, season_number, episode_number)
        else:
            # Check for movie
            query = '''
                SELECT state FROM media_items
                WHERE imdb_id = ?
            '''
            params = (imdb_id,)

        cursor = conn.execute(query, params)
        result = cursor.fetchone()
        conn.close()

        return result['state'] if result else "Missing"
    except Exception as e:
        logger.error(f"Error retrieving media item status: {e}")
        return "Missing"
    finally:
        conn.close()
        
def verify_database():
    create_tables()
    logger.info("Database verified and tables created if not exists.")

def get_all_media_items(state=None, media_type=None):
    conn = get_db_connection()
    query = 'SELECT * FROM media_items WHERE 1=1'
    params = []
    if state:
        query += ' AND state = ?'
        params.append(state)
    if media_type:
        query += ' AND type = ?'
        params.append(media_type)
    cursor = conn.execute(query, params)
    items = cursor.fetchall()
    conn.close()
    return items

def search_movies(search_term):
    conn = get_db_connection()
    cursor = conn.execute('SELECT * FROM media_items WHERE type = "movie" AND title LIKE ?', (f'%{search_term}%',))
    items = cursor.fetchall()
    conn.close()
    return items

def search_tv_shows(search_term):
    conn = get_db_connection()
    cursor = conn.execute('SELECT * FROM media_items WHERE type = "episode" AND (title LIKE ? OR episode_title LIKE ?)', (f'%{search_term}%', f'%{search_term}%'))
    items = cursor.fetchall()
    conn.close()
    return items

def purge_database(content_type=None, state=None):
    conn = get_db_connection()
    try:
        query = 'DELETE FROM media_items WHERE 1=1'
        params = []

        if content_type != 'all':
            query += ' AND type = ?'
            params.append(content_type)

        if state != 'all':
            query += ' AND state = ?'
            params.append(state)

        logger.debug(f"Executing query: {query} with params: {params}")
        conn.execute(query, params)
        conn.commit()
        logger.info(f"Database purged successfully for type '{content_type}' and state '{state}'.")
    except Exception as e:
        logger.error(f"Error purging database: {e}")
    finally:
        conn.close()
    create_tables()

def update_media_item_state(item_id, state, filled_by_title=None, filled_by_magnet=None):
    conn = get_db_connection()
    try:
        conn.execute('''
            UPDATE media_items
            SET state = ?, filled_by_title = ?, filled_by_magnet = ?, last_updated = ?
            WHERE id = ?
        ''', (state, filled_by_title, filled_by_magnet, datetime.now(), item_id))
        conn.commit()
        logger.debug(f"Updated media item (ID: {item_id}) state to {state}")
    except Exception as e:
        logger.error(f"Error updating media item (ID: {item_id}): {str(e)}")
    finally:
        conn.close()

def remove_from_media_items(item_id):
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM media_items WHERE id = ?', (item_id,))
        conn.commit()
        logger.info(f"Removed item (ID: {item_id}) from media items")
    except Exception as e:
        logger.error(f"Error removing item (ID: {item_id}) from media items: {str(e)}")
    finally:
        conn.close()

def get_title_by_imdb_id(imdb_id: str) -> str:
    logger.info(f"Looking up title for IMDb ID: {imdb_id}")
    conn = get_db_connection()
    cursor = conn.execute('''
        SELECT title FROM media_items WHERE imdb_id = ?
        UNION
        SELECT title FROM media_items WHERE tmdb_id = ?
    ''', (imdb_id, imdb_id))
    result = cursor.fetchone()
    conn.close()
    return result['title'] if result else None

def get_item_state(item_id: int) -> str:
    conn = get_db_connection()
    try:
        cursor = conn.execute('SELECT state FROM media_items WHERE id = ?', (item_id,))
        result = cursor.fetchone()
        if result:
            return result['state']
        else:
            logger.warning(f"No state found for item ID: {item_id}")
            return None
    except Exception as e:
        logger.error(f"Error getting state for item ID: {item_id}: {str(e)}")
        return None
    finally:
        conn.close()
