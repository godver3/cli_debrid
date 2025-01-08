import logging
import os
from typing import Dict, Any, Optional
from .core import get_db_connection
from utilities.plex_functions import force_match_with_tmdb

def find_matching_item_in_db(unmatched_title: str, unmatched_filename: str) -> Optional[Dict[str, Any]]:
    """
    Check if there's a matching item in the database based on filename.
    
    Args:
        unmatched_title (str): The title of the unmatched item
        unmatched_filename (str): The filename of the unmatched item
        
    Returns:
        Optional[Dict[str, Any]]: The matching item from database if found, None otherwise
    """
    try:
        conn = get_db_connection()
        unmatched_basename = os.path.basename(unmatched_filename)
        
        # Search in both filled_by_file and location_on_disk fields
        query = '''
            SELECT *
            FROM media_items
            WHERE (filled_by_file = ? OR location_on_disk = ? OR
                  filled_by_file LIKE ? OR location_on_disk LIKE ?)
            AND (imdb_id IS NOT NULL OR tmdb_id IS NOT NULL)
            ORDER BY last_updated DESC
            LIMIT 1
        '''
        
        # Parameters for exact match and for path-based match
        params = [
            unmatched_basename,
            unmatched_basename,
            f'%/{unmatched_basename}',
            f'%/{unmatched_basename}'
        ]
        
        cursor = conn.execute(query, params)
        item = cursor.fetchone()
        
        if item:
            item_dict = dict(item)
            logging.info(f"Found matching item in database for unmatched item '{unmatched_title}' - "
                        f"Database item has imdb_id: {item_dict.get('imdb_id')}, tmdb_id: {item_dict.get('tmdb_id')}")
            
            # Try to force match in Plex if we have a TMDB ID
            if item_dict.get('tmdb_id'):
                # Use filled_by_file as it contains the full filename with extension
                filled_by_file = item_dict.get('filled_by_file')
                if filled_by_file:
                    logging.debug(f"Using filename from filled_by_file: {filled_by_file}")
                    force_match_with_tmdb(filled_by_file, str(item_dict['tmdb_id']))
                else:
                    logging.warning("No filled_by_file found in database entry")
                
            return item_dict
                
        logging.debug(f"No matching item found in database for unmatched item: {unmatched_title}")
        return None
        
    except Exception as e:
        logging.error(f"Error while searching for matching database item: {str(e)}", exc_info=True)
        return None
    finally:
        conn.close() 