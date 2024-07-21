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

def add_or_update_media_items_batch(media_items_batch, status='Collected', full_scan=False):
    conn = get_db_connection()
    try:
        added_items_count = 0
        # Keep track of items to determine which ones to remove in full_scan mode
        processed_items = set()

        for index, item in enumerate(media_items_batch):
            try:
                normalized_title = normalize_string(item.get('title', 'Unknown'))
                if 'season_number' in item and 'episode_number' in item:
                    # It's an episode
                    existing_episode = conn.execute('''
                        SELECT id, release_date FROM media_items
                        WHERE imdb_id = ? AND season_number = ? AND episode_number = ?
                    ''', (item['imdb_id'], item['season_number'], item['episode_number'])).fetchone()

                    if not existing_episode:
                        conn.execute('''
                            INSERT INTO media_items
                            (imdb_id, tmdb_id, title, year, release_date, state, type, season_number, episode_number, episode_title, last_updated)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            item['imdb_id'], item['tmdb_id'], normalized_title, item['year'], item.get('release_date', None), status, 'episode',
                            item['season_number'], item['episode_number'], item.get('episode_title', ''), datetime.now()
                        ))
                        logger.debug(f"Adding episode to DB: {normalized_title} S{item['season_number']}E{item['episode_number']}")
                        added_items_count += 1
                    else:
                        # Update the state and release date if it already exists
                        conn.execute('''
                            UPDATE media_items
                            SET state = ?, release_date = ?, last_updated = ?
                            WHERE id = ?
                        ''', (status, item.get('release_date', existing_episode['release_date']), datetime.now(), existing_episode['id']))
                    processed_items.add((item['imdb_id'], item['season_number'], item['episode_number']))

                else:
                    # It's a movie
                    existing_movie = conn.execute('''
                        SELECT id, release_date FROM media_items
                        WHERE imdb_id = ? AND year = ?
                    ''', (item['imdb_id'], item['year'])).fetchone()

                    if not existing_movie:
                        conn.execute('''
                            INSERT INTO media_items
                            (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (item['imdb_id'], item['tmdb_id'], normalized_title, item['year'], item.get('release_date', None), status, 'movie', datetime.now()))
                        logger.debug(f"Adding movie to DB: {normalized_title}")
                        added_items_count += 1
                    else:
                        # Update the state and release date if it already exists
                        conn.execute('''
                            UPDATE media_items
                            SET state = ?, release_date = ?, last_updated = ?
                            WHERE id = ?
                        ''', (status, item.get('release_date', existing_movie['release_date']), datetime.now(), existing_movie['id']))
                    processed_items.add((item['imdb_id'], item['year']))

                if index % 50 == 0:
                    logger.info(f"Processed {index + 1}/{len(media_items_batch)} items")

            except KeyError as ke:
                logger.error(f"KeyError processing item {item.get('title', 'Unknown')}: {str(ke)}")
                logger.debug(f"Item not processed due to KeyError: {item}")
            except Exception as e:
                logger.error(f"Error processing item {item.get('title', 'Unknown')}: {str(e)}")
                logger.debug(f"Item not processed due to exception: {item}")

        if full_scan:
            logger.info("Performing full scan to remove outdated items.")
            # Fetch all collected items
            collected_items = conn.execute('SELECT id, imdb_id, season_number, episode_number, year FROM media_items WHERE state = ?', ('Collected',)).fetchall()

            for collected_item in collected_items:
                if collected_item['season_number'] is not None and collected_item['episode_number'] is not None:
                    item_tuple = (collected_item['imdb_id'], collected_item['season_number'], collected_item['episode_number'])
                else:
                    item_tuple = (collected_item['imdb_id'], collected_item['year'])

                if item_tuple not in processed_items:
                    conn.execute('DELETE FROM media_items WHERE id = ?', (collected_item['id'],))
                    logger.debug(f"Removed outdated collected item from DB: {collected_item['imdb_id']}")

        conn.commit()
        logger.info(f"Successfully processed batch of {len(media_items_batch)} items, added {added_items_count} new items")
    except Exception as e:
        logger.error(f"Error processing batch of items: {str(e)}")
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

def search_media_items(search_term):
    conn = get_db_connection()
    cursor = conn.execute('SELECT * FROM media_items WHERE title LIKE ? OR episode_title LIKE ?', (f'%{search_term}%', f'%{search_term}%'))
    items = cursor.fetchall()
    conn.close()
    return items

def purge_database(content_type=None, state=None):
    conn = get_db_connection()
    try:
        query = 'DELETE FROM media_items WHERE 1=1'
        params = []

        if content_type is not None:
            query += ' AND type = ?'
            params.append(content_type)

        if state is not None:
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
