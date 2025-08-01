from .core import get_db_connection, retry_on_db_lock
import logging
from datetime import datetime
import json
import pickle
from pathlib import Path
import os
from utilities.post_processing import handle_state_change
from typing import List
import sqlite3

@retry_on_db_lock()
def bulk_delete_by_id(id_value, id_type):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(f'DELETE FROM media_items WHERE {id_type} = ?', (id_value,))
        deleted_count = cursor.rowcount
        conn.commit()
        return deleted_count
    except sqlite3.OperationalError as e:
        logging.debug(f"OperationalError in bulk_delete_by_id: {e}. Handing over to retry_on_db_lock.")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in bulk_delete_by_id after OperationalError: {rb_ex}")
        raise
    except sqlite3.Error as e:
        logging.error(f"SQLite error bulk deleting items with {id_type.upper()} {id_value}: {str(e)}")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in bulk_delete_by_id after sqlite3.Error: {rb_ex}")
        return 0
    except Exception as e:
        logging.error(f"Unexpected error bulk deleting items with {id_type.upper()} {id_value}: {str(e)}")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in bulk_delete_by_id after non-Operational error: {rb_ex}")
        return 0
    finally:
        if conn:
            conn.close()

@retry_on_db_lock()
def update_year(item_id: int, year: int):
    conn = get_db_connection()
    try:
        conn.execute('''
            UPDATE media_items
            SET year = ?, last_updated = ?
            WHERE id = ?
        ''', (year, datetime.now(), item_id))
        conn.commit()
        logging.info(f"Updated year to {year} for item ID {item_id}")
        return True
    except sqlite3.OperationalError as e:
        logging.debug(f"OperationalError in update_year for item ID {item_id}: {e}. Handing over to retry_on_db_lock.")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_year after OperationalError: {rb_ex}")
        raise
    except sqlite3.Error as e:
        logging.error(f"SQLite error updating year for item ID {item_id}: {str(e)}")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_year after sqlite3.Error: {rb_ex}")
        return False
    except Exception as e:
        logging.error(f"Error updating year for item ID {item_id}: {str(e)}")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_year after non-Operational error: {rb_ex}")
        return False
    finally:
        if conn:
            conn.close()

@retry_on_db_lock()
def update_release_date_and_state(
        item_id: int, 
        release_date: str | None, 
        state: str, 
        airtime: str | None = None, 
        early_release: bool | None = None, 
        physical_release_date: str | None = None,
        theatrical_release_date: str | None = None,
        no_early_release: bool | None = None  # Add the new flag parameter
    ):
    """Update the release date, state, and potentially airtime, early_release, physical_release_date, and no_early_release flag for a media item."""
    conn = get_db_connection()
    try:
        conn.execute('BEGIN TRANSACTION')

        # Build the query dynamically
        set_clauses = [
            'release_date = ?', 
            'state = ?',
            'last_updated = ?'
        ]
        params = [release_date, state, datetime.now()]

        if airtime is not None:
            set_clauses.append('airtime = ?')
            params.append(airtime)
        
        if early_release is not None:
            set_clauses.append('early_release = ?')
            params.append(early_release)

        if physical_release_date is not None:
            set_clauses.append('physical_release_date = ?')
            params.append(physical_release_date)
            
        if theatrical_release_date is not None:
            set_clauses.append('theatrical_release_date = ?')
            params.append(theatrical_release_date)
            
        if no_early_release is not None:
            set_clauses.append('no_early_release = ?')
            params.append(no_early_release)

        params.append(item_id)

        query = f'''
            UPDATE media_items
            SET {', '.join(set_clauses)}
            WHERE id = ?
        '''
        conn.execute(query, params)
        
        # Fetch the updated item to check its state
        updated_item_row = conn.execute('SELECT * FROM media_items WHERE id = ?', (item_id,)).fetchone()

        conn.commit()

        logging.debug(f"Updated media item (ID: {item_id}) state to {state}")
        
        return dict(updated_item_row) if updated_item_row else None

    except sqlite3.OperationalError as e:
        logging.debug(f"OperationalError in update_release_date_and_state for item ID {item_id}: {e}. Handing over to retry_on_db_lock.")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_release_date_and_state after OperationalError: {rb_ex}")
        raise
    except sqlite3.Error as e:
        logging.error(f"SQLite error updating media item (ID: {item_id}): {str(e)}")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_release_date_and_state after sqlite3.Error: {rb_ex}")
        return None
    except Exception as e:
        logging.error(f"Error updating media item (ID: {item_id}): {str(e)}")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_release_date_and_state after non-Operational error: {rb_ex}")
        return None
    finally:
        if conn:
            conn.close()
    
@retry_on_db_lock()
def update_media_item_state(item_id, state, **kwargs):
    conn = get_db_connection()
    try:
        conn.execute('BEGIN TRANSACTION')
        
        # Get the item before update for post-processing
        item_before = conn.execute('SELECT * FROM media_items WHERE id = ?', (item_id,)).fetchone()
        
        # Prepare the base query
        query = '''
            UPDATE media_items
            SET state = ?, last_updated = ?
        '''
        params = [state, datetime.now()]

        # Add optional fields to the query if they are provided
        optional_fields = ['filled_by_title', 'filled_by_magnet', 'filled_by_file', 'filled_by_torrent_id', 'scrape_results', 'version', 'resolution', 'upgrading_from']
        for field in optional_fields:
            if field in kwargs:
                query += f", {field} = ?"
                value = kwargs[field]
                if field == 'scrape_results':
                    value = json.dumps(value) if value else None
                params.append(value)

        # Complete the query
        query += " WHERE id = ?"
        params.append(item_id)

        # Execute the query
        conn.execute(query, params)

        if state == 'Scraping':
            item = conn.execute('SELECT * FROM media_items WHERE id = ?', (item_id,)).fetchone()
            if item:
                #TODO: add_to_upgrading(dict(item))
                pass

        conn.commit()

        # Get updated item for post-processing
        updated_item_row = conn.execute('SELECT * FROM media_items WHERE id = ?', (item_id,)).fetchone()
        if updated_item_row:
            item_dict = dict(updated_item_row)
            
            # Handle post-processing based on state
            if state == 'Collected':
                handle_state_change(item_dict)
            elif state == 'Upgrading':
                handle_state_change(item_dict)

        logging.debug(f"Updated media item (ID: {item_id}) state to {state}")
        
        return dict(updated_item_row) if updated_item_row else None

    except sqlite3.OperationalError as e:
        logging.debug(f"OperationalError in update_media_item_state for item ID {item_id}: {e}. Handing over to retry_on_db_lock.")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_media_item_state after OperationalError: {rb_ex}")
        raise
    except sqlite3.Error as e:
        logging.error(f"SQLite error updating media item (ID: {item_id}): {str(e)}")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_media_item_state after sqlite3.Error: {rb_ex}")
        return None
    except Exception as e:
        logging.error(f"Error updating media item (ID: {item_id}): {str(e)}")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_media_item_state after non-Operational error: {rb_ex}")
        return None
    finally:
        if conn:
            conn.close()
    
@retry_on_db_lock()
def remove_from_media_items(item_id):
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM media_items WHERE id = ?', (item_id,))
        conn.commit()
        logging.info(f"Removed item (ID: {item_id}) from media items")
        return True
    except sqlite3.OperationalError as e:
        logging.debug(f"OperationalError in remove_from_media_items for item ID {item_id}: {e}. Handing over to retry_on_db_lock.")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in remove_from_media_items after OperationalError: {rb_ex}")
        raise
    except sqlite3.Error as e:
        logging.error(f"SQLite error removing item (ID: {item_id}) from media items: {str(e)}")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in remove_from_media_items after sqlite3.Error: {rb_ex}")
        return False
    except Exception as e:
        logging.error(f"Error removing item (ID: {item_id}) from media items: {str(e)}")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in remove_from_media_items after non-Operational error: {rb_ex}")
        return False
    finally:
        if conn:
            conn.close()

def add_to_collected_notifications(media_item):
    # Get db_content directory from environment variable with fallback
    db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    notifications_file = Path(db_content_dir) / "collected_notifications.pkl"
    
    try:
        os.makedirs(notifications_file.parent, exist_ok=True)
        
        if notifications_file.exists():
            with open(notifications_file, "rb") as f:
                notifications = pickle.load(f)
        else:
            notifications = []
        
        notifications.append(media_item)
        
        with open(notifications_file, "wb") as f:
            pickle.dump(notifications, f)
        
        logging.debug(f"Added notification for collected item: {media_item['title']} (ID: {media_item['id']})")
    except Exception as e:
        logging.error(f"Error adding notification for collected item (ID: {media_item['id']}): {str(e)}")

@retry_on_db_lock()
def update_media_item(item_id: int, **kwargs):
    conn = get_db_connection()
    try:
        # Build the SET clause dynamically from kwargs
        set_clause = ', '.join(f"{key} = ?" for key in kwargs.keys())
        params = list(kwargs.values())
        params.append(datetime.now())  # For 'last_updated'
        params.append(item_id)

        query = f'''
            UPDATE media_items
            SET {set_clause}, last_updated = ?
            WHERE id = ?
        '''

        conn.execute(query, params)
        conn.commit()

        logging.info(f"Updated media item ID {item_id} with values: {kwargs}")
        return True
    except sqlite3.OperationalError as e:
        logging.debug(f"OperationalError in update_media_item for item ID {item_id}: {e}. Handing over to retry_on_db_lock.")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_media_item after OperationalError: {rb_ex}")
        raise
    except sqlite3.Error as e:
        logging.error(f"SQLite error updating media item ID {item_id}: {str(e)}")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_media_item after sqlite3.Error: {rb_ex}")
        return False
    except Exception as e:
        logging.error(f"Error updating media item ID {item_id}: {str(e)}")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_media_item after non-Operational error: {rb_ex}")
        return False
    finally:
        if conn:
            conn.close()

@retry_on_db_lock()
def update_blacklisted_date(item_id: int, blacklisted_date: datetime | None):
    conn = get_db_connection()
    try:
        conn.execute('''
            UPDATE media_items
            SET blacklisted_date = ?, last_updated = ?
            WHERE id = ?
        ''', (blacklisted_date, datetime.now(), item_id))
        conn.commit()
        logging.info(f"Updated blacklisted_date to {blacklisted_date} for item ID {item_id}")
        return True
    except sqlite3.OperationalError as e:
        logging.debug(f"OperationalError in update_blacklisted_date for item ID {item_id}: {e}. Handing over to retry_on_db_lock.")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_blacklisted_date after OperationalError: {rb_ex}")
        raise
    except sqlite3.Error as e:
        logging.error(f"SQLite error updating blacklisted date for item ID {item_id}: {str(e)}")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_blacklisted_date after sqlite3.Error: {rb_ex}")
        return False
    except Exception as e:
        logging.error(f"Error updating blacklisted date for item ID {item_id}: {str(e)}")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_blacklisted_date after non-Operational error: {rb_ex}")
        return False
    finally:
        if conn:
            conn.close()

@retry_on_db_lock()
def update_anime_format(tmdb_id: str, format_type: str) -> bool:
    """Update the preferred anime format for all episodes of a show.
    
    Args:
        tmdb_id: The TMDB ID of the show
        format_type: The format type ('regular', 'absolute', or 'combined')
    Returns:
        bool: True if successful, False otherwise.
    """
    conn = get_db_connection()
    try:
        conn.execute('''
            UPDATE media_items
            SET anime_format = ?, last_updated = ?
            WHERE tmdb_id = ? AND type = 'episode'
        ''', (format_type, datetime.now(), tmdb_id))
        conn.commit()
        logging.info(f"Updated anime_format to {format_type} for show with TMDB ID {tmdb_id}")
        return True
    except sqlite3.OperationalError as e: 
        logging.debug(f"OperationalError in update_anime_format for TMDB ID {tmdb_id}: {e}. Handing over to retry_on_db_lock.")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_anime_format after OperationalError: {rb_ex}")
        raise
    except sqlite3.Error as e: 
        logging.error(f"SQLite error updating anime_format for TMDB ID {tmdb_id}: {str(e)}")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_anime_format after sqlite3.Error: {rb_ex}")
        return False
    except Exception as e: 
        logging.error(f"Unexpected error updating anime_format for TMDB ID {tmdb_id}: {str(e)}")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_anime_format after Exception: {rb_ex}")
        return False
    finally:
        if conn: 
            conn.close()

def get_anime_format(tmdb_id: str) -> str | None:
    """Get the preferred anime format for a show.
    
    Args:
        tmdb_id: The TMDB ID of the show
        
    Returns:
        str | None: The preferred format type or None if not set
    """
    conn = get_db_connection()
    try:
        cursor = conn.execute('''
            SELECT anime_format
            FROM media_items
            WHERE tmdb_id = ? AND type = 'episode'
            LIMIT 1
        ''', (tmdb_id,))
        result = cursor.fetchone()
        return result['anime_format'] if result else None
    except Exception as e:
        logging.error(f"Error getting anime_format for TMDB ID {tmdb_id}: {str(e)}")
        return None
    finally:
        conn.close()

@retry_on_db_lock()
def update_preferred_alias(tmdb_id: str, imdb_id: str, alias: str, media_type: str, season_number: int = None) -> bool:
    """Update the preferred alias for a movie or show.
    
    Args:
        tmdb_id: The TMDB ID of the media
        imdb_id: The IMDB ID of the media
        alias: The preferred alias to use
        media_type: The type of media ('movie' or 'episode')
        season_number: The season number (only for TV shows)
    Returns:
        bool: True if successful, False otherwise.
    """
    conn = get_db_connection()
    try:
        if media_type == 'episode':
            # For TV shows, update only the specific season
            conn.execute('''
                UPDATE media_items
                SET preferred_alias = ?, last_updated = ?
                WHERE tmdb_id = ? AND type = 'episode' AND season_number = ?
            ''', (alias, datetime.now(), tmdb_id, season_number))
        else:
            # For movies, update the specific movie
            conn.execute('''
                UPDATE media_items
                SET preferred_alias = ?, last_updated = ?
                WHERE tmdb_id = ? AND imdb_id = ? AND type = 'movie'
            ''', (alias, datetime.now(), tmdb_id, imdb_id))
        conn.commit()
        logging.info(f"Updated preferred_alias to '{alias}' for {'show season ' + str(season_number) if media_type == 'episode' else 'movie'} with TMDB ID {tmdb_id}")
        return True
    except sqlite3.OperationalError as e: 
        logging.debug(f"OperationalError in update_preferred_alias for TMDB ID {tmdb_id}: {e}. Handing over to retry_on_db_lock.")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_preferred_alias after OperationalError: {rb_ex}")
        raise
    except sqlite3.Error as e: 
        logging.error(f"SQLite error updating preferred_alias for TMDB ID {tmdb_id}: {str(e)}")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_preferred_alias after sqlite3.Error: {rb_ex}")
        return False
    except Exception as e: 
        logging.error(f"Unexpected error updating preferred_alias for TMDB ID {tmdb_id}: {str(e)}")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_preferred_alias after Exception: {rb_ex}")
        return False
    finally:
        if conn: 
            conn.close()

def get_preferred_alias(tmdb_id: str, imdb_id: str = None, media_type: str = None, season_number: int = None) -> str | None:
    """Get the preferred alias for a movie or show.
    
    Args:
        tmdb_id: The TMDB ID of the media
        imdb_id: The IMDB ID of the media (required for movies)
        media_type: The type of media ('movie' or 'episode')
        season_number: The season number (only for TV shows)
        
    Returns:
        str | None: The preferred alias or None if not set
    """
    conn = get_db_connection()
    try:
        if media_type == 'episode':
            cursor = conn.execute('''
                SELECT preferred_alias
                FROM media_items
                WHERE tmdb_id = ? AND type = 'episode' AND season_number = ?
                LIMIT 1
            ''', (tmdb_id, season_number))
        else:
            cursor = conn.execute('''
                SELECT preferred_alias
                FROM media_items
                WHERE tmdb_id = ? AND imdb_id = ? AND type = 'movie'
                LIMIT 1
            ''', (tmdb_id, imdb_id))
        result = cursor.fetchone()
        return result['preferred_alias'] if result else None
    except Exception as e:
        logging.error(f"Error getting preferred_alias for TMDB ID {tmdb_id}: {str(e)}")
        return None
    finally:
        conn.close()

@retry_on_db_lock()
def add_media_item(item: dict) -> int:
    """Add a new media item to the database.
    
    Args:
        item: Dictionary containing the media item data
        
    Returns:
        int: The ID of the newly inserted item, or None if insertion failed
    """
    conn = get_db_connection()
    try:
        # Get the column names from the item dictionary
        columns = list(item.keys())
        placeholders = ['?' for _ in columns]
        values = [item[col] for col in columns]
        
        # Add last_updated column
        columns.append('last_updated')
        placeholders.append('?')
        values.append(datetime.now())
        
        # Build and execute the INSERT query
        query = f'''
            INSERT INTO media_items ({', '.join(columns)})
            VALUES ({', '.join(placeholders)})
        '''
        cursor = conn.execute(query, values)
        item_id = cursor.lastrowid
        conn.commit()
        
        logging.info(f"Added new media item to database with ID {item_id}")
        return item_id
    except sqlite3.OperationalError as e:
        logging.debug(f"OperationalError in add_media_item: {e}. Handing over to retry_on_db_lock.")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in add_media_item after OperationalError: {rb_ex}")
        raise
    except sqlite3.Error as e:
        logging.error(f"SQLite error adding media item to database: {str(e)}")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in add_media_item after sqlite3.Error: {rb_ex}")
        return None
    except Exception as e:
        logging.error(f"Unexpected error adding media item to database: {str(e)}")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in add_media_item after Exception: {rb_ex}")
        return None
    finally:
        if conn:
            conn.close()

@retry_on_db_lock()
def update_version_name(old_version: str, new_version: str) -> int:
    """Update all media items with a specific version name to use a new version name,
    preserving any trailing characters (like asterisks).
    
    Args:
        old_version: The current version name prefix to update
        new_version: The new version name prefix to set
        
    Returns:
        int: Number of items updated
    """
    conn = get_db_connection()
    try:
        conn.execute('BEGIN TRANSACTION')
        
        # Construct the LIKE pattern to match versions starting with old_version
        like_pattern = f"{old_version}%" 
        
        # Use SQLite's SUBSTR and LENGTH to preserve trailing characters
        cursor = conn.execute("""
            UPDATE media_items
            SET version = ? || SUBSTR(version, LENGTH(?) + 1), 
                last_updated = ?
            WHERE version LIKE ?
        """, (new_version, old_version, datetime.now(), like_pattern))
        
        updated_count = cursor.rowcount
        conn.commit()
        logging.info(f"Updated version prefix from '{old_version}' to '{new_version}' for {updated_count} media items (preserving suffixes)")
        return updated_count
    except sqlite3.OperationalError as e:
        logging.debug(f"OperationalError in update_version_name from '{old_version}' to '{new_version}': {e}. Handing over to retry_on_db_lock.")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_version_name after OperationalError: {rb_ex}")
        raise
    except sqlite3.Error as e:
        # conn.rollback() # Rollback is implicitly handled by the transaction context if commit isn't reached
        logging.error(f"SQLite error updating version name prefix from '{old_version}' to '{new_version}': {str(e)}")
        try:
            if conn: conn.rollback() # Explicit rollback for clarity and safety
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_version_name after sqlite3.Error: {rb_ex}")
        return 0
    except Exception as e:
        # conn.rollback() # Rollback is implicitly handled
        logging.error(f"Unexpected error updating version name prefix from '{old_version}' to '{new_version}': {str(e)}")
        try:
            if conn: conn.rollback() # Explicit rollback
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_version_name after Exception: {rb_ex}")
        return 0
    finally:
        if conn:
            conn.close()

@retry_on_db_lock()
def update_version_for_items(old_version_id: str, new_version_id: str | None) -> int:
    """Update the version for all media items matching the old version ID.
    
    Args:
        old_version_id: The version ID to find and replace.
        new_version_id: The new version ID to set (can be None to make items versionless).
        
    Returns:
        The number of rows updated.
    """
    conn = get_db_connection()
    updated_count = 0
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE media_items
            SET version = ?, last_updated = ?
            WHERE version = ?
        """, (new_version_id, datetime.now(), old_version_id))
        updated_count = cursor.rowcount
        conn.commit()
        logging.info(f"Reassigned {updated_count} items from version '{old_version_id}' to '{new_version_id or 'None'}'")
    except sqlite3.OperationalError as e:
        logging.debug(f"OperationalError in update_version_for_items: {e}. Handing over to retry_on_db_lock.")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_version_for_items after OperationalError: {rb_ex}")
        raise
    except sqlite3.Error as e:
        logging.error(f"SQLite error updating items from version '{old_version_id}' to '{new_version_id}': {str(e)}")
        try:
            if conn: conn.rollback() 
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_version_for_items after sqlite3.Error: {rb_ex}")
        return 0 
    except Exception as e:
        logging.error(f"Unexpected error updating items from version '{old_version_id}' to '{new_version_id}': {str(e)}")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_version_for_items after Exception: {rb_ex}")
        return 0
    finally:
        if conn:
            conn.close()
    return updated_count

@retry_on_db_lock()
def update_media_items_state_batch(item_ids: List[int], state: str, **kwargs):
    """Update the state of multiple media items in a single transaction.
    
    Args:
        item_ids: List of item IDs to update
        state: New state for all items
        **kwargs: Additional fields to update
    """
    conn = get_db_connection()
    try:
        conn.execute('BEGIN TRANSACTION')
        
        # Prepare the base query
        query = '''
            UPDATE media_items
            SET state = ?, last_updated = ?
        '''
        base_params = [state, datetime.now()]

        # Add optional fields to the query
        optional_fields = ['filled_by_title', 'filled_by_magnet', 'filled_by_file', 
                         'filled_by_torrent_id', 'scrape_results', 'version', 
                         'resolution', 'upgrading_from']
        
        for field in kwargs:
            if field in optional_fields:
                query += f", {field} = ?"
                value = kwargs[field]
                if field == 'scrape_results':
                    value = json.dumps(value) if value else None
                base_params.append(value)

        # Complete the query with ID list
        placeholders = ','.join('?' * len(item_ids))
        query += f" WHERE id IN ({placeholders})"
        params = base_params + item_ids

        # Execute the batch update
        conn.execute(query, params)
        conn.commit()

        # Get updated items for post-processing
        for item_id in item_ids:
            updated_item = conn.execute('SELECT * FROM media_items WHERE id = ?', (item_id,)).fetchone()
            if updated_item:
                item_dict = dict(updated_item)
                if state in ['Collected', 'Upgrading']:
                    handle_state_change(item_dict)

        logging.info(f"Batch updated {len(item_ids)} items to state {state}")
    except sqlite3.OperationalError as e:
        logging.debug(f"OperationalError in update_media_items_state_batch: {e}. Handing over to retry_on_db_lock.")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_media_items_state_batch after OperationalError: {rb_ex}")
        raise
    except sqlite3.Error as e:
        logging.error(f"SQLite error in batch state update: {str(e)}")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_media_items_state_batch after sqlite3.Error: {rb_ex}")
        # This function does not return a value, so no return here on error.
    except Exception as e:
        logging.error(f"Unexpected error in batch state update: {str(e)}")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_media_items_state_batch after Exception: {rb_ex}")
        # This function does not return a value, so no return here on error.
    finally:
        if conn:
            conn.close()

@retry_on_db_lock()
def update_media_item_torrent_id(item_id: int, new_torrent_id: str) -> bool:
    """Updates the 'filled_by_torrent_id' for a specific media item."""
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "UPDATE media_items SET filled_by_torrent_id = ? WHERE id = ?",
            (new_torrent_id, item_id)
        )
        conn.commit()
        if cursor.rowcount > 0:
            logging.info(f"Updated filled_by_torrent_id for media item {item_id} to {new_torrent_id}")
            return True
        else:
            logging.warning(f"No media item found with id {item_id} to update torrent ID.")
            return False
    except sqlite3.OperationalError as e:
        logging.debug(f"OperationalError in update_media_item_torrent_id for item {item_id}: {e}. Handing over to retry_on_db_lock.")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_media_item_torrent_id after OperationalError: {rb_ex}")
        raise
    except sqlite3.Error as e:
        logging.error(f"Database error updating torrent ID for item {item_id}: {e}")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_media_item_torrent_id after sqlite3.Error: {rb_ex}")
        return False
    except Exception as e:
        logging.error(f"Unexpected error updating torrent ID for item {item_id}: {e}")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_media_item_torrent_id after Exception: {rb_ex}")
        return False
    finally:
        if conn:
            conn.close()

@retry_on_db_lock()
def set_wake_count(item_id: int, wake_count: int):
    """Set the wake count for a specific media item."""
    conn = get_db_connection()
    try:
        conn.execute('''
            UPDATE media_items
            SET wake_count = ?, last_updated = ?
            WHERE id = ?
        ''', (wake_count, datetime.now(), item_id))
        conn.commit()
        # logging.debug(f"Set wake_count to {wake_count} for item ID {item_id}")
    except sqlite3.OperationalError as e:
        logging.debug(f"OperationalError in set_wake_count for item {item_id}: {e}. Handing over to retry_on_db_lock.")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in set_wake_count after OperationalError: {rb_ex}")
        raise
    except sqlite3.Error as e:
        logging.error(f"SQLite error setting wake_count for item ID {item_id}: {str(e)}")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in set_wake_count after sqlite3.Error: {rb_ex}")
        # No return value for this function on error, implicitly None
    except Exception as e:
        logging.error(f"Unexpected error setting wake_count for item ID {item_id}: {str(e)}")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in set_wake_count after Exception: {rb_ex}")
        # No return value for this function on error, implicitly None
    finally:
        if conn:
            conn.close()

@retry_on_db_lock()
def increment_wake_count(item_id: int) -> int:
    """Increment the wake count for a specific media item and return the new count."""
    conn = get_db_connection()
    new_wake_count = 0
    try:
        # Ensure atomicity
        conn.execute('BEGIN IMMEDIATE TRANSACTION')
        
        # Get current count
        cursor = conn.execute('SELECT wake_count FROM media_items WHERE id = ?', (item_id,))
        result = cursor.fetchone()
        current_wake_count = result['wake_count'] if result and result['wake_count'] is not None else 0
        
        # Increment
        new_wake_count = current_wake_count + 1
        
        # Update
        conn.execute('''
            UPDATE media_items
            SET wake_count = ?, last_updated = ?
            WHERE id = ?
        ''', (new_wake_count, datetime.now(), item_id))
        
        conn.commit()
        # logging.debug(f"Incremented wake_count to {new_wake_count} for item ID {item_id}")
        return new_wake_count
    except sqlite3.OperationalError as e:
        logging.debug(f"OperationalError in increment_wake_count for item {item_id}: {e}. Handing over to retry_on_db_lock.")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in increment_wake_count after OperationalError: {rb_ex}")
        raise
    except sqlite3.Error as e:
        # conn.rollback() # Handled by transaction context or explicit rollback
        logging.error(f"SQLite error incrementing wake_count for item ID {item_id}: {str(e)}")
        try:
            if conn: conn.rollback() # Explicit rollback
        except Exception as rb_ex:
            logging.error(f"Rollback failed in increment_wake_count after sqlite3.Error: {rb_ex}")
        return 0 
    except Exception as e:
        # conn.rollback() # Handled by transaction context or explicit rollback
        logging.error(f"Unexpected error incrementing wake_count for item ID {item_id}: {str(e)}")
        try:
            if conn: conn.rollback() # Explicit rollback
        except Exception as rb_ex:
            logging.error(f"Rollback failed in increment_wake_count after Exception: {rb_ex}")
        return 0 
    finally:
        if conn:
            conn.close()