from .core import get_db_connection
import logging
from typing import List
from datetime import datetime

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