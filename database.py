from sqlite3 import Row
import sqlite3
from typing import Dict, Any, List
import logging
from datetime import datetime, timedelta
import unicodedata
import json
from manual_blacklist import is_blacklisted

def get_db_connection():
    conn = sqlite3.connect('db_content/media_items.db')
    conn.row_factory = sqlite3.Row
    return conn

def normalize_string(input_str):
    return ''.join(
        c for c in unicodedata.normalize('NFKD', input_str)
        if unicodedata.category(c) != 'Mn'
    )

def row_to_dict(row: Row) -> Dict[str, Any]:
    return {key: row[key] for key in row.keys()}

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
            metadata_updated TIMESTAMP,
            sleep_cycles INTEGER DEFAULT 0,
            last_checked TIMESTAMP,
            scrape_results TEXT,
            UNIQUE(imdb_id, tmdb_id, title, year, season_number, episode_number)
        )
    ''')
    
    # Add new columns if they don't exist
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(media_items)")
    columns = [column[1] for column in cursor.fetchall()]
    
    if 'sleep_cycles' not in columns:
        conn.execute('ALTER TABLE media_items ADD COLUMN sleep_cycles INTEGER DEFAULT 0')
    if 'last_checked' not in columns:
        conn.execute('ALTER TABLE media_items ADD COLUMN last_checked TIMESTAMP')
    if 'scrape_results' not in columns:  # Add this line
        conn.execute('ALTER TABLE media_items ADD COLUMN scrape_results TEXT')  # Add this line
        
    conn.commit()
    conn.close()

def update_media_item_sleep_cycle(item_id, sleep_cycles):
    conn = get_db_connection()
    try:
        conn.execute('''
            UPDATE media_items
            SET sleep_cycles = ?, last_checked = ?
            WHERE id = ?
        ''', (sleep_cycles, datetime.now(), item_id))
        conn.commit()
        logging.debug(f"Updated sleep cycle for media item (ID: {item_id}) to {sleep_cycles}")
    except Exception as e:
        logging.error(f"Error updating sleep cycle for media item (ID: {item_id}): {str(e)}")
    finally:
        conn.close()


def get_metadata_updated(imdb_id=None, tmdb_id=None, title=None, year=None, season_number=None, episode_number=None):
    conn = get_db_connection()
    try:
        if season_number is not None and episode_number is not None:
            # Check for TV show episode
            query = '''
                SELECT metadata_updated FROM media_items
                WHERE imdb_id = ? AND season_number = ? AND episode_number = ?
            '''
            params = (imdb_id, season_number, episode_number)
        else:
            # Check for movie
            query = '''
                SELECT metadata_updated FROM media_items
                WHERE imdb_id = ?
            '''
            params = (imdb_id,)

        cursor = conn.execute(query, params)
        result = cursor.fetchone()
        return result['metadata_updated'] if result else None
    except Exception as e:
        logging.error(f"Error retrieving metadata_updated: {e}")
        return None
    finally:
        conn.close()

def update_metadata(item, metadata_date=None):
    conn = get_db_connection()
    try:
        if metadata_date is None:
            metadata_date = datetime.now()

        if 'season_number' in item and 'episode_number' in item:
            # It's an episode
            conn.execute('''
                UPDATE media_items
                SET tmdb_id = ?, title = ?, year = ?, release_date = ?, episode_title = ?, metadata_updated = ?
                WHERE imdb_id = ? AND season_number = ? AND episode_number = ?
            ''', (
                item['tmdb_id'], item['title'], item['year'], item.get('release_date', None),
                item.get('episode_title', ''), metadata_date,
                item['imdb_id'], item['season_number'], item['episode_number']
            ))
        else:
            # It's a movie
            conn.execute('''
                UPDATE media_items
                SET tmdb_id = ?, title = ?, year = ?, release_date = ?, metadata_updated = ?
                WHERE imdb_id = ?
            ''', (
                item['tmdb_id'], item['title'], item['year'], item.get('release_date', None),
                metadata_date, item['imdb_id']
            ))

        conn.commit()
        logging.debug(f"Updated metadata for {item['title']}")
    except Exception as e:
        logging.error(f"Error updating metadata: {str(e)}")
    finally:
        conn.close()

def is_metadata_stale(metadata_date_str):
    try:
        # Try parsing with fractional seconds
        metadata_date = datetime.strptime(metadata_date_str, '%Y-%m-%d %H:%M:%S.%f')
    except ValueError:
        # Fall back to parsing without fractional seconds
        metadata_date = datetime.strptime(metadata_date_str, '%Y-%m-%d %H:%M:%S')
    
    return (datetime.now() - metadata_date) > timedelta(days=7)

def create_database():
    create_tables()
    logging.info("Database created and tables initialized.")


def add_wanted_items(media_items_batch: List[Dict[str, Any]]):
    conn = get_db_connection()
    try:
        # Get all existing items from the database
        existing_items = conn.execute('''
            SELECT imdb_id, type, season_number, episode_number 
            FROM media_items
        ''').fetchall()
        
        existing_set = set()
        for item in map(row_to_dict, existing_items):
            if item['type'] == 'movie':
                existing_set.add((item['imdb_id'], 'movie'))
            else:
                existing_set.add((item['imdb_id'], 'episode', item['season_number'], item['episode_number']))

        items_added = 0
        items_skipped = 0

        for item in media_items_batch:
            if not item.get('imdb_id'):
                logging.warning(f"Skipping item without valid IMDb ID: {item.get('title', 'Unknown')}")
                items_skipped += 1
                continue
            
            if is_blacklisted(item['imdb_id']):
                logging.debug(f"Skipping blacklisted item: {item.get('title', 'Unknown')} (IMDb ID: {item['imdb_id']})")
                items_skipped += 1
                continue

            normalized_title = normalize_string(item.get('title', 'Unknown'))
            item_type = 'episode' if 'season_number' in item and 'episode_number' in item else 'movie'
            
            if item_type == 'movie':
                item_key = (item['imdb_id'], 'movie')
            else:
                item_key = (item['imdb_id'], 'episode', item['season_number'], item['episode_number'])

            if item_key not in existing_set:
                # Insert new item as Wanted
                if item_type == 'movie':
                    conn.execute('''
                        INSERT INTO media_items
                        (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        item['imdb_id'], item.get('tmdb_id'), normalized_title, item.get('year'),
                        item.get('release_date', 'Unknown'), 'Wanted', 'movie', datetime.now()
                    ))
                else:
                    conn.execute('''
                        INSERT INTO media_items
                        (imdb_id, tmdb_id, title, year, release_date, state, type, season_number, episode_number, episode_title, last_updated)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        item['imdb_id'], item.get('tmdb_id'), normalized_title, item.get('year'),
                        item.get('release_date', 'Unknown'), 'Wanted', 'episode',
                        item['season_number'], item['episode_number'], item.get('episode_title', ''),
                        datetime.now()
                    ))
                if item_type == 'movie':
                    logging.info(f"Adding new movie as Wanted in DB: {normalized_title} ({item.get('year', 'Unknown')})")
                else:
                    logging.info(f"Adding new episode as Wanted in DB: {normalized_title} S{item['season_number']}E{item['episode_number']}")
                items_added += 1
            else:
                logging.debug(f"Skipping {'movie' if item_type == 'movie' else 'episode'} as it already exists in DB: {normalized_title}")
                items_skipped += 1

        conn.commit()
        logging.info(f"Wanted items processing complete. Added: {items_added}")
    except Exception as e:
        logging.error(f"Error adding wanted items: {str(e)}")
        conn.rollback()
    finally:
        conn.close()

def add_collected_items(media_items_batch, recent=False):
    conn = get_db_connection()
    try:
        processed_items = set()

        # Get all existing items from the database
        existing_items = conn.execute('SELECT id, imdb_id, type, season_number, episode_number, state FROM media_items').fetchall()
        existing_ids = {}
        for row in map(row_to_dict, existing_items):
            if row['type'] == 'movie':
                key = (row['imdb_id'], 'movie')
            else:
                key = (row['imdb_id'], 'episode', row['season_number'], row['episode_number'])
            existing_ids[key] = (row['id'], row['state'])

        # Process incoming items
        for item in media_items_batch:
            if not item.get('imdb_id'):
                logging.warning(f"Skipping item without valid IMDb ID: {item.get('title', 'Unknown')}")
                continue

            normalized_title = normalize_string(item.get('title', 'Unknown'))
            item_type = 'episode' if 'season_number' in item and 'episode_number' in item else 'movie'

            if item_type == 'movie':
                item_key = (item['imdb_id'], 'movie')
            else:
                item_key = (item['imdb_id'], 'episode', item['season_number'], item['episode_number'])

            if item_key in existing_ids:
                # Update existing item
                item_id, current_state = existing_ids[item_key]
                if item_type == 'episode':
                    conn.execute('''
                        UPDATE media_items
                        SET tmdb_id = ?, title = ?, year = ?, release_date = ?, state = ?, episode_title = ?, last_updated = ?, metadata_updated = ?, scrape_results = ?
                        WHERE id = ?
                    ''', (
                        item.get('tmdb_id'), normalized_title, item.get('year'), item.get('release_date', 'Unknown'), 'Collected',
                        item.get('episode_title', ''), datetime.now(), datetime.now(), item.get('scrape_results', None), item_id
                    ))
                    logging.debug(f"Updating episode in DB: {normalized_title} S{item['season_number']}E{item['episode_number']}")
                    if current_state == 'Checking':
                        logging.info(f"Episode {normalized_title} S{item['season_number']}E{item['episode_number']} has been collected")
                else:
                    conn.execute('''
                        UPDATE media_items
                        SET tmdb_id = ?, title = ?, year = ?, release_date = ?, state = ?, last_updated = ?, metadata_updated = ?, scrape_results = ?
                        WHERE id = ?
                    ''', (
                        item.get('tmdb_id'), normalized_title, item.get('year'), item.get('release_date', 'Unknown'), 'Collected',
                        datetime.now(), datetime.now(), item.get('scrape_results', None), item_id
                    ))
                    logging.debug(f"Updating movie in DB: {normalized_title}")
                    if current_state == 'Checking':
                        logging.info(f"Movie {normalized_title} ({item.get('year', 'Unknown')}) has been collected")
                processed_items.add(item_id)
            else:
                # Insert new item
                if item_type == 'episode':
                    cursor = conn.execute('''
                        INSERT INTO media_items
                        (imdb_id, tmdb_id, title, year, release_date, state, type, season_number, episode_number, episode_title, last_updated, metadata_updated, scrape_results)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ''', (
                        item['imdb_id'], item.get('tmdb_id'), normalized_title, item.get('year'), item.get('release_date', 'Unknown'), 'Collected', 'episode',
                        item['season_number'], item['episode_number'], item.get('episode_title', ''), datetime.now(), datetime.now(), item.get('scrape_results', None)
                    ))
                    logging.info(f"Adding new episode to DB: {normalized_title} S{item['season_number']}E{item['episode_number']}")
                else:
                    cursor = conn.execute('''
                        INSERT INTO media_items
                        (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated, metadata_updated, scrape_results)
                        VALUES (?,?,?,?,?,?,?,?,?,?)
                    ''', (
                        item['imdb_id'], item.get('tmdb_id'), normalized_title, item.get('year'), item.get('release_date', 'Unknown'), 'Collected', 'movie',
                        datetime.now(), datetime.now(), item.get('scrape_results', None)
                    ))
                    logging.info(f"Adding new movie to DB: {normalized_title}")
                processed_items.add(cursor.lastrowid)

        # Remove items not in the batch if not recent
        if not recent:
            items_to_check = set(id for id, _ in existing_ids.values()) - processed_items
            for item_id in items_to_check:
                # Fetch the title and state before deciding what to do
                item_info = conn.execute('SELECT title, state FROM media_items WHERE id = ?', (item_id,)).fetchone()
                if item_info:
                    title, state = item_info
                else:
                    title, state = "Unknown", "Unknown"

                if state == 'Collected':
                    conn.execute('DELETE FROM media_items WHERE id = ?', (item_id,))
                    logging.info(f"Removing Collected item from DB: ID {item_id}, {title}")
                else:
                    logging.debug(f"Keeping item in DB (status not Collected): ID {item_id}, {title}, Status: {state}")

        conn.commit()
        logging.debug(f"Collected items processed and database updated. Total items: {len(media_items_batch)}, Processed: {len(processed_items)}")
    except Exception as e:
        logging.error(f"Error adding collected items: {str(e)}", exc_info=True)
        conn.rollback()
    finally:
        conn.close()

def get_media_item_by_id(item_id):
    conn = get_db_connection()
    try:
        cursor = conn.execute('SELECT * FROM media_items WHERE id = ?', (item_id,))
        item = cursor.fetchone()
        return dict(item) if item else None
    except Exception as e:
        logging.error(f"Error retrieving media item (ID: {item_id}): {str(e)}")
        return None
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
        logging.error(f"Error retrieving media item status: {e}")
        return "Missing"
    finally:
        conn.close()
        
def verify_database():
    create_tables()
    logging.info("Database verified and tables created if not exists.")

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

        if state == 'working':
            query += ' AND state NOT IN (?, ?)'
            params.extend(['Wanted', 'Collected', 'Blacklisted'])
        elif state != 'all':
            query += ' AND state = ?'
            params.append(state)

        logging.debug(f"Executing query: {query} with params: {params}")
        conn.execute(query, params)
        conn.commit()
        logging.info(f"Database purged successfully for type '{content_type}' and state '{state}'.")
    except Exception as e:
        logging.error(f"Error purging database: {e}")
    finally:
        conn.close()
    create_tables()

def update_media_item_state(item_id, state, filled_by_title=None, filled_by_magnet=None, scrape_results=None):
    conn = get_db_connection()
    try:
        scrape_results_str = json.dumps(scrape_results) if scrape_results else None  # Convert list to JSON string
        conn.execute('''
            UPDATE media_items
            SET state =?, filled_by_title =?, filled_by_magnet =?, scrape_results =?, last_updated =?
            WHERE id =?
        ''', (state, filled_by_title, filled_by_magnet, scrape_results_str, datetime.now(), item_id))
        conn.commit()
        logging.debug(f"Updated media item (ID: {item_id}) state to {state}, filled by to {filled_by_magnet}")
    except Exception as e:
        logging.error(f"Error updating media item (ID: {item_id}): {str(e)}")
    finally:
        conn.close()

def remove_from_media_items(item_id):
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM media_items WHERE id = ?', (item_id,))
        conn.commit()
        logging.info(f"Removed item (ID: {item_id}) from media items")
    except Exception as e:
        logging.error(f"Error removing item (ID: {item_id}) from media items: {str(e)}")
    finally:
        conn.close()

def get_title_by_imdb_id(imdb_id: str) -> str:
    logging.info(f"Looking up title for IMDb ID: {imdb_id}")
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
            logging.warning(f"No state found for item ID: {item_id}")
            return None
    except Exception as e:
        logging.error(f"Error getting state for item ID: {item_id}: {str(e)}")
        return None
    finally:
        conn.close()

def get_blacklisted_items():
    conn = get_db_connection()
    try:
        cursor = conn.execute('SELECT * FROM media_items WHERE state = "Blacklisted"')
        items = cursor.fetchall()
        return [dict(item) for item in items]
    except Exception as e:
        logging.error(f"Error retrieving blacklisted items: {str(e)}")
        return []
    finally:
        conn.close()

def remove_from_blacklist(item_ids: List[int]):
    conn = get_db_connection()
    try:
        for item_id in item_ids:
            conn.execute('''
                UPDATE media_items
                SET state = 'Wanted', last_updated = ?, sleep_cycles = 0
                WHERE id = ? AND state = 'Blacklisted'
            ''', (datetime.now(), item_id))
        conn.commit()
        logging.info(f"Removed {len(item_ids)} items from blacklist")
    except Exception as e:
        logging.error(f"Error removing items from blacklist: {str(e)}")
        conn.rollback()
    finally:
        conn.close()
        
def update_release_date_and_state(item_id, release_date, new_state):
    conn = get_db_connection()
    try:
        conn.execute('''
            UPDATE media_items
            SET release_date = ?, state = ?, last_updated = ?
            WHERE id = ?
        ''', (release_date, new_state, datetime.now(), item_id))
        conn.commit()
        logging.info(f"Updated release date to {release_date} and state to {new_state} for item ID {item_id}")
    except Exception as e:
        logging.error(f"Error updating release date and state for item ID {item_id}: {str(e)}")
    finally:
        conn.close()
