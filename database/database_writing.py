from .core import get_db_connection, retry_on_db_lock
import logging
from datetime import datetime
import json
import pickle
from pathlib import Path

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

def update_release_date_and_state(item_id, release_date, new_state):
    conn = get_db_connection()
    try:
        # First, fetch the current item data
        cursor = conn.execute('SELECT * FROM media_items WHERE id = ?', (item_id,))
        item = cursor.fetchone()
        
        if item:
            conn.execute('''
                UPDATE media_items
                SET release_date = ?, state = ?, last_updated = ?
                WHERE id = ?
            ''', (release_date, new_state, datetime.now(), item_id))
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
        
        # Prepare the base query
        query = '''
            UPDATE media_items
            SET state = ?, last_updated = ?
        '''
        params = [state, datetime.now()]

        # Add optional fields to the query if they are provided
        optional_fields = ['filled_by_title', 'filled_by_magnet', 'filled_by_file', 'filled_by_torrent_id', 'scrape_results', 'version']
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

        logging.debug(f"Updated media item (ID: {item_id}) state to {state}")
        for field in optional_fields:
            if field in kwargs:
                logging.debug(f"  {field}: {kwargs[field]}")

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
    notifications_file = Path("/user/db_content/collected_notifications.pkl")
    
    try:
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

