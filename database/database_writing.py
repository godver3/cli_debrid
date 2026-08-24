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

# original_collected_at records an item's first-ever collection time and must
# never be reset without also resetting collected_at (and vice versa) — every
# site that sends an item back to 'Wanted' for a genuine re-download needs
# both cleared together, or the item permanently loses its "first collection"
# notification (see queue_manager.move_to_collected and local_library_scan.py,
# which both key off original_collected_at being unset). Interpolate this
# into each reset UPDATE's SET clause instead of hand-typing the two columns.
RESET_COLLECTION_STATE_SQL = "collected_at = NULL, original_collected_at = NULL"

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
def update_media_item_state(item_id, state, skip_state_change_hook=False, **kwargs):
    conn = get_db_connection()
    try:
        conn.execute('BEGIN TRANSACTION')

        # Prepare the base query
        query = '''
            UPDATE media_items
            SET state = ?, last_updated = ?
        '''
        params = [state, datetime.now()]

        # Add optional fields to the query if they are provided
        optional_fields = [
            'filled_by_title',
            'filled_by_magnet',
            'filled_by_file',
            'filled_by_torrent_id',
            'scrape_results',
            'version',
            'resolution',
            'upgrading_from',
            'debrid_folder_name',
            'original_filename',
        ]
        for field in optional_fields:
            if field in kwargs:
                query += f", {field} = ?"
                value = kwargs[field]
                if field == 'scrape_results':
                    value = json.dumps(value) if value else None
                params.append(value)

        # Always clear scrape_results when transitioning to a terminal state —
        # scrape_results is only needed while adding/checking and can grow to
        # hundreds of MB if left on collected/blacklisted items.
        if state in ('Collected', 'Blacklisted', 'Ghostlisted', 'Unreleased') and 'scrape_results' not in kwargs:
            query += ", scrape_results = NULL"

        # Complete the query
        query += " WHERE id = ?"
        params.append(item_id)

        # Execute the query
        conn.execute(query, params)

        # Get updated item for post-processing (while still in transaction)
        updated_item_row = conn.execute('SELECT * FROM media_items WHERE id = ?', (item_id,)).fetchone()

        # Commit BEFORE post-processing to release lock quickly
        conn.commit()

        # Post-processing AFTER commit (lock is released)
        if updated_item_row:
            item_dict = dict(updated_item_row)

            # Handle post-processing based on state. Callers that already ran
            # handle_state_change() for this exact state transition themselves
            # (e.g. checking_queue.py, after local_library_scan.py's explicit
            # call) pass skip_state_change_hook=True to avoid double-running
            # CineSync, the subtitle downloader, and any custom post-processing
            # script for the same item in the same cycle.
            if not skip_state_change_hook:
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
    """
    Permanently delete item from media_items table

    Args:
        item_id: Database ID of the item to delete

    Returns:
        True if successful, False otherwise
    """
    conn = get_db_connection()
    try:
        conn.execute('BEGIN TRANSACTION')

        # Verify the item exists before deletion
        cursor = conn.execute('SELECT id FROM media_items WHERE id = ?', (item_id,))
        item_exists = cursor.fetchone()

        if not item_exists:
            logging.warning(f"Item (ID: {item_id}) not found in database - cannot delete")
            conn.rollback()
            return False

        # Perform the deletion - this deletes the ENTIRE row
        result = conn.execute('DELETE FROM media_items WHERE id = ?', (item_id,))
        deleted_count = result.rowcount

        if deleted_count == 0:
            logging.warning(f"Item (ID: {item_id}) deletion returned 0 rows affected")
            conn.rollback()
            return False

        conn.commit()
        logging.info(f"Successfully deleted item (ID: {item_id}) from media_items - {deleted_count} row(s) removed")
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

def delete_items_batch(item_ids, blacklist=False):
    """
    Delete or blacklist multiple items in a single transaction

    Args:
        item_ids: List of database IDs to delete
        blacklist: If True, update state to Blacklisted instead of deleting

    Returns:
        dict with keys:
            - success: bool
            - deleted_count: int
            - error: str or None
            - database_locked: bool (only if lock detected)
    """
    from database.core import get_db_connection

    if not item_ids:
        return {'success': False, 'deleted_count': 0, 'error': 'No items provided'}

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # BEGIN TRANSACTION
        cursor.execute('BEGIN TRANSACTION')

        placeholders = ','.join('?' * len(item_ids))

        if blacklist:
            # Batch UPDATE - set state to Blacklisted
            query = f"""
                UPDATE media_items
                SET state = 'Blacklisted'
                WHERE id IN ({placeholders})
            """
            cursor.execute(query, item_ids)
            operation = 'blacklisted'
        else:
            # Batch DELETE - permanently remove from database
            query = f"""
                DELETE FROM media_items
                WHERE id IN ({placeholders})
            """
            cursor.execute(query, item_ids)
            operation = 'deleted'

        affected_rows = cursor.rowcount

        # COMMIT TRANSACTION
        conn.commit()

        logging.info(f"Batch {operation} {affected_rows} items from media_items (requested: {len(item_ids)})")

        return {
            'success': True,
            'deleted_count': affected_rows,
            'error': None
        }

    except sqlite3.OperationalError as e:
        # Rollback and check for database lock
        try:
            conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in delete_items_batch after OperationalError: {rb_ex}")

        if "database is locked" in str(e):
            logging.error(f"Database locked during batch deletion of {len(item_ids)} items")
            return {
                'success': False,
                'deleted_count': 0,
                'error': 'database is locked',
                'database_locked': True
            }
        else:
            logging.error(f"OperationalError in delete_items_batch: {e}")
            return {
                'success': False,
                'deleted_count': 0,
                'error': str(e)
            }

    except sqlite3.Error as e:
        # Rollback on SQLite error
        try:
            conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in delete_items_batch after sqlite3.Error: {rb_ex}")

        logging.error(f"SQLite error in batch deletion of {len(item_ids)} items: {e}")
        return {
            'success': False,
            'deleted_count': 0,
            'error': str(e)
        }

    except Exception as e:
        # Rollback on any other error
        try:
            conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in delete_items_batch after Exception: {rb_ex}")

        logging.error(f"Unexpected error in batch deletion of {len(item_ids)} items: {e}")
        return {
            'success': False,
            'deleted_count': 0,
            'error': str(e)
        }
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
            WHERE id = ? AND (ghostlisted IS NULL OR ghostlisted = 0)
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

def enable_fallback_to_single_scraper(item: dict, reason: str = ""):
    """Sets fall_back_to_single_scraper=True for `item` and every other pending
    episode of the same series/season/version, regardless of episode number.

    Multi-pack mode (queues/scraping_queue.py) is forced on for any episode
    scrape older than 7 days - a single stuck-in-multi episode (e.g. episode 1,
    since nothing "before" it can ever propagate the flag to it) can otherwise
    be starved of single-episode candidates forever if the only multi-pack
    result available gets filtered out for an unrelated reason (bad language,
    wrong group, etc.), since forcing multi-pack rejects every single-episode
    result outright. Previously this only propagated to *later* episodes
    (episode_number > current), which is exactly what let episode 1 get stuck.
    """
    item_id = item.get('id')
    if not item_id or item.get('fall_back_to_single_scraper'):
        return
    update_media_item(item_id, fall_back_to_single_scraper=True)
    logging.info(f"Enabled single scraper fallback for item ID {item_id} ({item.get('title')}){f': {reason}' if reason else ''}")

    if item.get('type') != 'episode':
        return
    series_title = item.get('series_title', '') or item.get('title', '')
    season = item.get('season') or item.get('season_number')
    version = item.get('version')
    current_id = item_id

    from .database_reading import stream_all_media_items
    for candidate in stream_all_media_items(state=None, media_type='episode'):
        try:
            if candidate.get('id') == current_id:
                continue
            if (candidate.get('series_title', '') or candidate.get('title', '')) != series_title:
                continue
            if (candidate.get('season') or candidate.get('season_number')) != season:
                continue
            if candidate.get('version') != version:
                continue
            if candidate.get('fall_back_to_single_scraper'):
                continue
            match_id = candidate.get('id')
            if match_id:
                update_media_item(match_id, fall_back_to_single_scraper=True)
                logging.debug(f"Enabled single scraper fallback for related item ID: {match_id} ({candidate.get('title')})")
        except Exception as iter_err:
            logging.error(f"Error while streaming candidate items for single scraper fallback: {iter_err}")

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
def add_media_item(item: dict, user_initiated: bool = False) -> int:
    """Add a new media item to the database.

    Args:
        item: Dictionary containing the media item data
        user_initiated: If True, bypasses ghostlist/blacklist checks for manual user actions.
                       When bypassing, uses version-aware logic:
                       - Same version: Updates existing entry (unghosts + updates torrent info)
                       - Different version: Inserts new entry (leaves old ghostlisted)

    Returns:
        int: The ID of the newly inserted item, or None if insertion failed
    """
    conn = get_db_connection()
    try:
        imdb_id = item.get('imdb_id')
        tmdb_id = item.get('tmdb_id')
        item_type = item.get('type')
        version = item.get('version')

        # Resolve imdb_id from tmdb_id if missing — ensures Plex GUID fast-path works
        # and prevents trial-and-error match loops for items added with only TMDB ID.
        if not imdb_id and tmdb_id:
            try:
                from cli_battery.app.direct_api import DirectAPI
                _resolved_imdb, _ = DirectAPI.tmdb_to_imdb(
                    str(tmdb_id),
                    media_type='show' if item_type in ('episode', 'show') else 'movie'
                )
                if _resolved_imdb:
                    imdb_id = _resolved_imdb
                    item['imdb_id'] = _resolved_imdb
                    logging.debug(f"[add_media_item] Resolved imdb_id {_resolved_imdb} from tmdb_id {tmdb_id}")
            except Exception:
                pass

        if item_type == 'movie':
            from .movie_release_overrides import apply_movie_release_override_to_item
            apply_movie_release_override_to_item(item, conn=conn)

        # GHOSTLIST/BLACKLIST CHECK
        if imdb_id or tmdb_id:
            # Build query to check for ghostlisted/blacklisted entries
            ghostlist_check_query = '''
                SELECT id, version FROM media_items
                WHERE (imdb_id = ? OR tmdb_id = ?)
                AND type = ?
                AND (ghostlisted = 1 OR state = 'Blacklisted')
                LIMIT 1
            '''
            ghostlist_check_params = [imdb_id, tmdb_id, item_type]

            # For episodes, also check season/episode number
            if item_type == 'episode' and 'season_number' in item and 'episode_number' in item:
                ghostlist_check_query = '''
                    SELECT id, version FROM media_items
                    WHERE (imdb_id = ? OR tmdb_id = ?)
                    AND type = ?
                    AND season_number = ?
                    AND episode_number = ?
                    AND (ghostlisted = 1 OR state = 'Blacklisted')
                    LIMIT 1
                '''
                ghostlist_check_params = [imdb_id, tmdb_id, item_type, item['season_number'], item['episode_number']]

            ghostlist_result = conn.execute(ghostlist_check_query, ghostlist_check_params).fetchone()

            if ghostlist_result:
                # If NOT user-initiated, block the addition (existing behavior)
                if not user_initiated:
                    logging.info(f"⛔ Skipping add_media_item - user has ghostlisted/blacklisted this item (ID: {ghostlist_result[0]}, IMDB: {imdb_id}, Type: {item_type})")
                    conn.close()
                    return None

                # User-initiated bypass: Check version to determine action
                existing_id = ghostlist_result[0]
                existing_version = ghostlist_result[1]

                # Same version: UPDATE existing entry (unghost + update torrent info)
                if existing_version == version:
                    logging.info(f"🔓 User-initiated add: Updating existing ghostlisted/blacklisted entry (ID: {existing_id}, Version: {version})")

                    # Build UPDATE query for relevant fields
                    update_fields = []
                    update_values = []

                    # Unghost and reset state
                    update_fields.append('ghostlisted = ?')
                    update_values.append(0)

                    # Update torrent-related fields if present
                    if 'state' in item:
                        update_fields.append('state = ?')
                        update_values.append(item['state'])
                    if 'magnet_link' in item:
                        update_fields.append('magnet_link = ?')
                        update_values.append(item['magnet_link'])
                    if 'scrape_results' in item:
                        update_fields.append('scrape_results = ?')
                        update_values.append(item['scrape_results'])
                    if 'torrent_name' in item:
                        update_fields.append('torrent_name = ?')
                        update_values.append(item['torrent_name'])

                    # Always update last_updated
                    update_fields.append('last_updated = ?')
                    update_values.append(datetime.now())

                    # Add WHERE clause parameter
                    update_values.append(existing_id)

                    update_query = f"UPDATE media_items SET {', '.join(update_fields)} WHERE id = ?"
                    conn.execute(update_query, update_values)
                    conn.commit()

                    logging.info(f"✅ Updated existing entry ID {existing_id} - unghosted and updated torrent info")
                    return existing_id

                # Different version: INSERT new entry (leave old ghostlisted alone)
                else:
                    logging.info(f"🔓 User-initiated add: Different version detected (existing: {existing_version}, new: {version}). Inserting new entry, leaving old ghostlisted entry (ID: {existing_id}) as-is.")

        # Sanitize year — never store the string "None", convert to None/int
        if 'year' in item:
            y = item['year']
            if y == 'None' or y == '' or y is None:
                # Try to recover from release_date
                rd = item.get('release_date', '')
                if rd and len(str(rd)) >= 4:
                    try:
                        item['year'] = int(str(rd)[:4])
                    except (ValueError, TypeError):
                        item['year'] = None
                else:
                    item['year'] = None
            elif isinstance(y, str) and y.isdigit():
                item['year'] = int(y)

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
                         'resolution', 'upgrading_from', 'debrid_folder_name']
        
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
def update_delayed_upgrade_eligibility(item_id: int, eligible: bool) -> bool:
    """Update the delayed upgrade eligibility for a specific media item."""
    conn = get_db_connection()
    try:
        conn.execute('''
            UPDATE media_items
            SET delayed_upgrade_eligible = ?, last_updated = ?
            WHERE id = ?
        ''', (eligible, datetime.now(), item_id))
        conn.commit()
        logging.debug(f"Updated delayed_upgrade_eligible to {eligible} for item ID {item_id}")
        return True
    except sqlite3.OperationalError as e:
        logging.debug(f"OperationalError in update_delayed_upgrade_eligibility for item {item_id}: {e}. Handing over to retry_on_db_lock.")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_delayed_upgrade_eligibility after OperationalError: {rb_ex}")
        raise
    except sqlite3.Error as e:
        logging.error(f"SQLite error updating delayed_upgrade_eligible for item ID {item_id}: {str(e)}")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_delayed_upgrade_eligibility after sqlite3.Error: {rb_ex}")
        return False
    except Exception as e:
        logging.error(f"Unexpected error updating delayed_upgrade_eligibility for item ID {item_id}: {str(e)}")
        try:
            if conn: conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed in update_delayed_upgrade_eligibility after Exception: {rb_ex}")
        return False
    finally:
        if conn:
            conn.close()

@retry_on_db_lock()
def get_delayed_upgrade_eligible_items(days_threshold: int) -> List[dict]:
    """Get items eligible for delayed upgrade scraping that were released exactly N days ago."""
    conn = get_db_connection()
    try:
        cursor = conn.execute('''
            SELECT * FROM media_items
            WHERE delayed_upgrade_eligible = 1
            AND release_date IS NOT NULL
            AND date(release_date) = date('now', '-{} days')
            AND state = 'Collected'
        '''.format(days_threshold))
        
        items = [dict(row) for row in cursor.fetchall()]
        logging.info(f"Found {len(items)} items eligible for delayed upgrade scraping (released exactly {days_threshold} days ago)")
        return items
    except Exception as e:
        logging.error(f"Error getting delayed upgrade eligible items: {str(e)}")
        return []
    finally:
        conn.close()

@retry_on_db_lock()
def increment_wake_count(item_id: int) -> int:
    """Increment the wake count for a specific media item and return the new count."""
    conn = get_db_connection()
    new_wake_count = 0
    try:
        # Ensure atomicity
        conn.execute('BEGIN TRANSACTION')

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

# =============================================================================
# Deletion Functions (for DeletionManager)
# =============================================================================

def update_item_state(item_id: int, new_state: str) -> bool:
    """
    Update item state (for blacklisting during deletion)

    Args:
        item_id: Database ID of the item
        new_state: New state to set (typically 'Blacklisted')

    Returns:
        True if successful, False otherwise
    """
    conn = get_db_connection()
    try:
        conn.execute('''
            UPDATE media_items
            SET state = ?,
                last_updated = ?
            WHERE id = ?
        ''', (new_state, datetime.now(), item_id))
        conn.commit()
        logging.info(f"Updated item {item_id} state to {new_state}")
        return True
    except Exception as e:
        logging.error(f"Error updating item state for {item_id}: {e}")
        try:
            if conn:
                conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed: {rb_ex}")
        return False
    finally:
        if conn:
            conn.close()

def cleanup_show_metadata(imdb_id: str) -> bool:
    """
    Clean up tv_shows table after show deletion
    Removes show metadata and version status tracking

    Args:
        imdb_id: IMDB identifier of the show

    Returns:
        True if successful, False otherwise
    """
    conn = get_db_connection()
    try:
        # Remove from tv_shows table
        conn.execute('DELETE FROM tv_shows WHERE imdb_id = ?', (imdb_id,))

        # Remove from tv_show_version_status table if it exists
        try:
            conn.execute('DELETE FROM tv_show_version_status WHERE imdb_id = ?', (imdb_id,))
        except sqlite3.OperationalError:
            # Table might not exist, that's okay
            pass

        conn.commit()
        logging.info(f"Cleaned up metadata for show {imdb_id}")
        return True
    except Exception as e:
        logging.error(f"Error cleaning up show metadata for {imdb_id}: {e}")
        try:
            if conn:
                conn.rollback()
        except Exception as rb_ex:
            logging.error(f"Rollback failed: {rb_ex}")
        return False
    finally:
        if conn:
            conn.close()
