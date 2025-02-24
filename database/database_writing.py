from .core import get_db_connection, retry_on_db_lock
import logging
from datetime import datetime
import json
import pickle
from pathlib import Path
import os
from utilities.post_processing import handle_state_change

def bulk_delete_by_id(id_value, id_type):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(f'DELETE FROM media_items WHERE {id_type} = ?', (id_value,))
        deleted_count = cursor.rowcount
        conn.commit()
        return deleted_count
    except Exception as e:
        logging.error(f"Error bulk deleting items with {id_type.upper()} {id_value}: {str(e)}")
        return 0
    finally:
        conn.close()

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
    except Exception as e:
        logging.error(f"Error updating year for item ID {item_id}: {str(e)}")
    finally:
        conn.close()

def update_release_date_and_state(item_id, release_date, new_state, early_release=None, physical_release_date=None):
    """Update the release date and state of a media item.
    
    Args:
        item_id: The ID of the media item to update
        release_date: The new release date
        new_state: The new state
        early_release: Optional flag for early release
        physical_release_date: Optional physical release date for movies
    """
    conn = get_db_connection()
    try:
        # First, fetch the current item data
        cursor = conn.execute('SELECT * FROM media_items WHERE id = ?', (item_id,))
        item = cursor.fetchone()
        
        if item:
            update_query = '''
                UPDATE media_items
                SET release_date = ?, state = ?, last_updated = ?
            '''
            params = [release_date, new_state, datetime.now()]
            
            if early_release is not None:
                update_query += ', early_release = ?'
                params.append(early_release)
                
            if physical_release_date is not None:
                update_query += ', physical_release_date = ?'
                params.append(physical_release_date)
                
            update_query += ' WHERE id = ?'
            params.append(item_id)
            
            conn.execute(update_query, params)
            conn.commit()
            
            # Create item description based on the type of media
            if item['type'] == 'episode':
                item_description = f"{item['title']} S{item['season_number']:02d}E{item['episode_number']:02d}"
            else:  # movie
                item_description = f"{item['title']} ({item['year']})"
            
            logging.debug(f"Updated release date to {release_date} and state to {new_state} for {item_description}")
        else:
            logging.error(f"No item found with ID {item_id}")
    except Exception as e:
        logging.error(f"Error updating release date and state for item ID {item_id}: {str(e)}")
    finally:
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
        updated_item = conn.execute('SELECT * FROM media_items WHERE id = ?', (item_id,)).fetchone()
        if updated_item:
            # Convert to dict for post-processing
            item_dict = dict(updated_item)
            
            # Handle post-processing based on state
            if state == 'Collected':
                handle_state_change(item_dict)
            elif state == 'Upgrading':
                handle_state_change(item_dict)

        logging.debug(f"Updated media item (ID: {item_id}) state to {state}")

    except Exception as e:
        logging.error(f"Error updating media item (ID: {item_id}): {str(e)}")
        conn.rollback()
        raise
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
    except Exception as e:
        logging.error(f"Error updating media item ID {item_id}: {str(e)}")
    finally:
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
    except Exception as e:
        logging.error(f"Error updating blacklisted_date for item ID {item_id}: {str(e)}")
        raise
    finally:
        conn.close()

@retry_on_db_lock()
def update_anime_format(tmdb_id: str, format_type: str):
    """Update the preferred anime format for all episodes of a show.
    
    Args:
        tmdb_id: The TMDB ID of the show
        format_type: The format type ('regular', 'absolute', or 'combined')
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
    except Exception as e:
        logging.error(f"Error updating anime_format for TMDB ID {tmdb_id}: {str(e)}")
        raise
    finally:
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
def update_preferred_alias(tmdb_id: str, imdb_id: str, alias: str, media_type: str, season_number: int = None):
    """Update the preferred alias for a movie or show.
    
    Args:
        tmdb_id: The TMDB ID of the media
        imdb_id: The IMDB ID of the media
        alias: The preferred alias to use
        media_type: The type of media ('movie' or 'episode')
        season_number: The season number (only for TV shows)
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
    except Exception as e:
        logging.error(f"Error updating preferred_alias for TMDB ID {tmdb_id}: {str(e)}")
        raise
    finally:
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
    except Exception as e:
        logging.error(f"Error adding media item to database: {str(e)}")
        return None
    finally:
        conn.close()

@retry_on_db_lock()
def update_version_name(old_version: str, new_version: str) -> int:
    """Update all media items with a specific version name to use a new version name.
    
    Args:
        old_version: The current version name to update
        new_version: The new version name to set
        
    Returns:
        int: Number of items updated
    """
    conn = get_db_connection()
    try:
        conn.execute('BEGIN TRANSACTION')
        cursor = conn.execute('''
            UPDATE media_items
            SET version = ?, last_updated = ?
            WHERE version = ?
        ''', (new_version, datetime.now(), old_version))
        updated_count = cursor.rowcount
        conn.commit()
        logging.info(f"Updated version from '{old_version}' to '{new_version}' for {updated_count} media items")
        return updated_count
    except Exception as e:
        conn.rollback()
        logging.error(f"Error updating version name from '{old_version}' to '{new_version}': {str(e)}")
        return 0
    finally:
        conn.close()