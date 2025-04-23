from .core import get_db_connection
import logging
import os
import json
from typing import List, Dict, Optional, Tuple, Set, Any
try:
    import tracemalloc
    tracemalloc_available = True
except ImportError:
    tracemalloc_available = False
    tracemalloc = None # Define tracemalloc as None if import failed
import time
from utilities.settings import get_setting

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

def get_all_media_items(state=None, media_type=None, imdb_id=None, tmdb_id=None, limit: Optional[int] = None):
    tracemalloc_enabled = get_setting('Debug', 'enable_tracemalloc', False)
    mem_before, peak_before = 0, 0
    start_time = time.time()
    items = []

    # Only attempt tracemalloc operations if it was imported and enabled
    if tracemalloc_available and tracemalloc_enabled:
        if tracemalloc.is_tracing():
            try:
                mem_before, peak_before = tracemalloc.get_traced_memory()
            except Exception as e_mem:
                logging.error(f"[Tracemalloc DB] Error getting memory before get_all_media_items: {e_mem}")
                # Disable further tracemalloc attempts for this call if get_traced_memory fails
                tracemalloc_enabled = False 
        else:
            # If tracing is not active, disable tracemalloc for this function call
            tracemalloc_enabled = False

    conn = None
    try:
        conn = get_db_connection()
        query = 'SELECT * FROM media_items WHERE 1=1'
        params = []
        if state:
            if isinstance(state, (list, tuple)):
                placeholders = ','.join(['?' for _ in state])
                query += f' AND state IN ({placeholders})'
                params.extend(state)
            else:
                query += ' AND state = ?'
                params.append(state)
        if media_type:
            query += ' AND type = ?'
            params.append(media_type)
        if imdb_id:
            query += ' AND imdb_id = ?'
            params.append(imdb_id)
        if tmdb_id:
            query += ' AND tmdb_id = ?'
            params.append(tmdb_id)

        if limit is not None and isinstance(limit, int) and limit > 0:
            query += ' LIMIT ?'
            params.append(limit)

        cursor = conn.execute(query, params)
        db_rows = cursor.fetchall()
        items = [dict(item) for item in db_rows]

    except Exception as e:
        logging.error(f"Error executing get_all_media_items query: {e}")
        logging.debug(f"Query: {query}, Params: {params}")
        items = []
    finally:
        if conn:
            conn.close()

    duration = time.time() - start_time
    # Check again if tracemalloc is available and was enabled for this call
    if tracemalloc_available and tracemalloc_enabled and tracemalloc.is_tracing():
        try:
            mem_after, peak_after = tracemalloc.get_traced_memory()
            mem_delta = mem_after - mem_before
            peak_delta = peak_after - peak_before

            log_level = logging.INFO if abs(mem_delta / (1024*1024)) < 5 else logging.WARNING
            logging.log(log_level,
                        f"[Tracemalloc DB] get_all_media_items completed in {duration:.3f}s. "
                        f"Returned Items: {len(items)}. "
                        f"Mem Delta: {mem_delta / (1024*1024):+.2f}MB ({mem_before / (1024*1024):.2f} -> {mem_after / (1024*1024):.2f}MB). "
                        f"Peak Delta During Call: {peak_delta / (1024*1024):+.2f}MB.")

        except Exception as e_mem:
            logging.error(f"[Tracemalloc DB] Error getting memory after get_all_media_items: {e_mem}")
    # else: Tracemalloc not available or not enabled, no memory logging needed

    return items

def get_media_item_presence(imdb_id=None, tmdb_id=None):
    conn = get_db_connection()
    try:
        # Determine the query and parameters based on provided IDs
        if imdb_id is not None:
            id_field = 'imdb_id'
            id_value = imdb_id
        elif tmdb_id is not None:
            id_field = 'tmdb_id'
            id_value = tmdb_id
        else:
            raise ValueError("Either imdb_id or tmdb_id must be provided.")

        # Check for a matching item in the database
        query = f'''
            SELECT state FROM media_items
            WHERE {id_field} = ?
        '''
        params = (id_value,)

        cursor = conn.execute(query, params)
        result = cursor.fetchone()

        return result['state'] if result else "Missing"
    except ValueError as ve:
        logging.error(f"Invalid input: {ve}")
        return "Missing"
    except Exception as e:
        logging.error(f"Error retrieving media item status: {e}")
        return "Missing"
    finally:
        conn.close()

def get_media_item_by_id(item_id):
    conn = get_db_connection()
    try:
        cursor = conn.execute('SELECT * FROM media_items WHERE id = ?', (item_id,))
        item = cursor.fetchone()
        if item:
            item_dict = dict(item)
            return item_dict
        return None
    except Exception as e:
        logging.error(f"Error retrieving media item (ID: {item_id}): {str(e)}")
        return None
    finally:
        conn.close()

def get_movie_runtime(tmdb_id):
    conn = get_db_connection()
    try:
        cursor = conn.execute('SELECT runtime FROM media_items WHERE tmdb_id = ? AND type = "movie"', (tmdb_id,))
        result = cursor.fetchone()
        return result['runtime'] if result else None
    except Exception as e:
        logging.error(f"Error retrieving movie runtime (TMDB ID: {tmdb_id}): {str(e)}")
        return None
    finally:
        conn.close()

def get_episode_runtime(tmdb_id):
    conn = get_db_connection()
    try:
        query = '''
            SELECT AVG(runtime) as runtime FROM media_items 
            WHERE tmdb_id = ? AND type = "episode"
        '''
        cursor = conn.execute(query, (tmdb_id,))
        result = cursor.fetchone()
        return result['runtime'] if result and result['runtime'] is not None else None
    except Exception as e:
        logging.error(f"Error retrieving episode runtime (TMDB ID: {tmdb_id}): {str(e)}")
        return None
    finally:
        conn.close()

def get_episode_count(tmdb_id):
    conn = get_db_connection()
    try:
        query = '''
            SELECT COUNT(*) as episode_count 
            FROM (
                SELECT DISTINCT season_number, episode_number, version
                FROM media_items 
                WHERE tmdb_id = ? AND type = "episode"
            )
        '''
        cursor = conn.execute(query, (tmdb_id,))
        result = cursor.fetchone()
        return result['episode_count'] if result else 0
    except Exception as e:
        logging.error(f"Error retrieving episode count (TMDB ID: {tmdb_id}): {str(e)}")
        return 0
    finally:
        conn.close()

def get_all_season_episode_counts(tmdb_id):
    conn = get_db_connection()
    try:
        query = '''
            SELECT season_number, COUNT(*) as episode_count
            FROM (
                SELECT DISTINCT season_number, episode_number, version
                FROM media_items
                WHERE tmdb_id = ? AND type = "episode"
            )
            GROUP BY season_number
            ORDER BY season_number
        '''
        cursor = conn.execute(query, (tmdb_id,))
        results = cursor.fetchall()
        return {row['season_number']: row['episode_count'] for row in results}
    except Exception as e:
        logging.error(f"Error retrieving season episode counts (TMDB ID: {tmdb_id}): {str(e)}")
        return {}
    finally:
        conn.close()

def row_to_dict(row):
    return dict(row)

def get_all_videos():
    """
    Retrieve all videos from the database with their essential information.
    Groups movies and TV shows separately.
    """
    conn = get_db_connection()
    try:
        # Get movies
        cursor = conn.execute('''
            SELECT 
                id,
                title,
                year,
                type as media_type,
                filled_by_file,
                location_on_disk,
                version,
                state
            FROM media_items
            WHERE type = 'movie'
            AND state = 'Collected'
            AND (location_on_disk IS NOT NULL OR filled_by_file IS NOT NULL)
            ORDER BY title, year
        ''')
        movies = [row_to_dict(row) for row in cursor]
        
        # Get TV episodes
        cursor = conn.execute('''
            SELECT 
                id,
                title,
                year,
                type as media_type,
                filled_by_file,
                location_on_disk,
                version,
                state,
                season_number,
                episode_number,
                episode_title
            FROM media_items
            WHERE type = 'episode'
            AND state = 'Collected'
            AND (location_on_disk IS NOT NULL OR filled_by_file IS NOT NULL)
            ORDER BY title, year, season_number, episode_number
        ''')
        episodes = [row_to_dict(row) for row in cursor]
        
        return {
            'movies': movies,
            'episodes': episodes
        }
    finally:
        conn.close()

def get_video_by_id(video_id):
    """
    Retrieve a specific video by its ID.
    """
    conn = get_db_connection()
    try:
        cursor = conn.execute('''
            SELECT id, title, filled_by_file, state, location_on_disk
            FROM media_items
            WHERE id = ?
        ''', (video_id,))
        row = cursor.fetchone()
        if row:
            video = row_to_dict(row)
            logging.info(f"Found video in database: {video}")
            # Use location_on_disk for file path if available
            if not video.get('location_on_disk') and video.get('filled_by_file'):
                logging.warning(f"No location_on_disk for video {video_id}, falling back to filled_by_file")
            return video
        logging.error(f"No video found with ID {video_id}")
        return None
    finally:
        conn.close()

def get_media_country_code(tmdb_id: str) -> str:
    """
    Get the country code for a media item by its TMDB ID.
    Returns None if no country code is found.
    """
    conn = get_db_connection()
    try:
        cursor = conn.execute('SELECT country FROM media_items WHERE tmdb_id = ? LIMIT 1', (tmdb_id,))
        result = cursor.fetchone()
        return result['country'] if result and result['country'] else None
    except Exception as e:
        logging.error(f"Error retrieving country code (TMDB ID: {tmdb_id}): {str(e)}")
        return None
    finally:
        conn.close()

def get_episode_details(imdb_id: str, season: int, episode: int) -> dict:
    """
    Get episode details including release date by IMDB ID, season, and episode number.
    Returns None if no episode is found.
    """
    conn = get_db_connection()
    try:
        cursor = conn.execute('''
            SELECT * FROM media_items 
            WHERE imdb_id = ? 
            AND season_number = ? 
            AND episode_number = ?
            AND type = 'episode'
            LIMIT 1
        ''', (imdb_id, season, episode))
        result = cursor.fetchone()
        return dict(result) if result else None
    except Exception as e:
        logging.error(f"Error retrieving episode details (IMDB ID: {imdb_id}, S{season:02d}E{episode:02d}): {str(e)}")
        return None
    finally:
        conn.close()

def get_imdb_aliases(imdb_id: str) -> List[str]:
    """
    Get all IMDB aliases for a given IMDB ID from the database.
    Returns a list of IMDB IDs including aliases.
    The aliases are stored in JSON string format, e.g. ["tt28251824"]
    """
    conn = get_db_connection()
    try:
        # First check if the imdb_id exists in the database
        cursor = conn.execute('''
            SELECT imdb_aliases FROM media_items 
            WHERE imdb_id = ? AND imdb_aliases IS NOT NULL
        ''', (imdb_id,))
        result = cursor.fetchone()
        
        if result and result['imdb_aliases']:
            try:
                # Parse the JSON string to get the list of aliases
                aliases = json.loads(result['imdb_aliases'])
                if imdb_id not in aliases:
                    aliases.append(imdb_id)
                return aliases
            except json.JSONDecodeError as e:
                logging.error(f"Error parsing IMDB aliases JSON for {imdb_id}: {str(e)}")
                return [imdb_id]
        return [imdb_id]  # Return just the original ID if no aliases found
    except Exception as e:
        logging.error(f"Error retrieving IMDB aliases for {imdb_id}: {str(e)}")
        return [imdb_id]
    finally:
        conn.close()

def get_media_items_by_ids_batch(item_ids: List[int]) -> List[Dict]:
    """Get multiple media items by their IDs in a single query.
    
    Args:
        item_ids: List of item IDs to retrieve
        
    Returns:
        List of media items as dictionaries
    """
    conn = get_db_connection()
    try:
        placeholders = ','.join('?' * len(item_ids))
        query = f'SELECT * FROM media_items WHERE id IN ({placeholders})'
        cursor = conn.execute(query, item_ids)
        items = cursor.fetchall()
        return [dict(item) for item in items]
    except Exception as e:
        logging.error(f"Error retrieving media items batch: {str(e)}")
        return []
    finally:
        conn.close()

def get_media_item_by_filename(filename: str) -> Optional[Dict]:
    """
    Retrieve a media item from the database based on its filename.

    Args:
        filename: The name of the file associated with the media item (filled_by_file column).

    Returns:
        A dictionary containing the media item's details, or None if not found.
    """
    conn = get_db_connection()
    try:
        # Select all columns for the matching filename
        # Using filled_by_file as the target column for the filename lookup
        query = 'SELECT * FROM media_items WHERE filled_by_file = ?'
        cursor = conn.execute(query, (filename,))
        item = cursor.fetchone()
        
        if item:
            item_dict = row_to_dict(item) # Use existing helper function
            # Ensure necessary fields for the webhook are present, even if null
            item_dict.setdefault('imdb_id', None)
            item_dict.setdefault('tmdb_id', None)
            item_dict.setdefault('tvdb_id', None) # Add tvdb_id default
            item_dict.setdefault('type', None)
            item_dict.setdefault('title', None)
            item_dict.setdefault('year', None)
            if item_dict.get('type') == 'episode':
                 item_dict.setdefault('season_number', None)
                 item_dict.setdefault('episode_number', None)
                 item_dict.setdefault('show_title', None) # Add show_title default (may need to derive this)
                 item_dict.setdefault('show_year', None) # Add show_year default (may need to derive this)
            return item_dict
        else:
            logging.debug(f"No media item found for filename: {filename}")
            return None
    except Exception as e:
        logging.error(f"Error retrieving media item by filename ({filename}): {str(e)}")
        return None
    finally:
        conn.close()

def check_existing_media_item(item_details: Dict, target_version: str, target_states: List[str]) -> bool:
    """
    Check if a media item already exists in the database with specific identifiers, 
    a target version, and one of the target states.

    Args:
        item_details: Dictionary containing identifying details (type, imdb_id, tmdb_id, season_number, episode_number).
        target_version: The version string to check for.
        target_states: A list of state strings (e.g., ['Collected', 'Upgrading']) to check for.

    Returns:
        True if a matching item exists in one of the target states, False otherwise.
    """
    conn = get_db_connection()
    try:
        base_query = 'SELECT 1 FROM media_items WHERE version = ? AND state IN ({})'.format(','.join('?' * len(target_states)))
        params = [target_version] + target_states
        
        item_type = item_details.get('type')
        
        # Prefer IMDB ID if available
        imdb_id = item_details.get('imdb_id')
        if imdb_id:
            base_query += ' AND imdb_id = ?'
            params.append(imdb_id)
        else:
            # Fallback to TMDB ID if IMDB ID is missing
            tmdb_id = item_details.get('tmdb_id')
            if tmdb_id:
                base_query += ' AND tmdb_id = ?'
                params.append(tmdb_id)
            else:
                logging.warning("Cannot check for existing item without imdb_id or tmdb_id.")
                return False # Cannot reliably check without an ID

        if item_type == 'episode':
            season_number = item_details.get('season_number')
            episode_number = item_details.get('episode_number')
            if season_number is not None and episode_number is not None:
                base_query += ' AND season_number = ? AND episode_number = ?'
                params.extend([season_number, episode_number])
            else:
                logging.warning(f"Cannot check for existing episode without season/episode number for ID {imdb_id or tmdb_id}.")
                return False # Cannot reliably check episode without season/episode
        elif item_type == 'movie':
            # No further fields needed for movies besides ID
            pass
        else:
            logging.warning(f"Unknown item type '{item_type}' for checking existing media.")
            return False

        base_query += ' LIMIT 1'
        
        cursor = conn.execute(base_query, params)
        result = cursor.fetchone()
        
        return result is not None

    except Exception as e:
        logging.error(f"Error checking for existing media item (Version: {target_version}, States: {target_states}): {str(e)}")
        logging.error(f"Item details used for check: {item_details}")
        return False # Assume not found on error
    finally:
        conn.close()

def get_wake_count(item_id: int) -> int:
    """Get the current wake count for a media item."""
    conn = get_db_connection()
    try:
        cursor = conn.execute('SELECT wake_count FROM media_items WHERE id = ?', (item_id,))
        result = cursor.fetchone()
        # Return the count, default to 0 if NULL or not found
        return result['wake_count'] if result and result['wake_count'] is not None else 0 
    except Exception as e:
        logging.error(f"Error retrieving wake_count for item ID {item_id}: {str(e)}")
        return 0 # Default to 0 on error
    finally:
        conn.close()

def get_show_episode_identifiers_from_db(imdb_id: Optional[str] = None, tmdb_id: Optional[str] = None) -> Set[Tuple[int, int]]:
    """
    Efficiently retrieve a set of unique (season_number, episode_number) tuples
    for a given show directly from the database. Prioritizes IMDb ID if both are provided.
    """
    if not imdb_id and not tmdb_id:
        logging.warning("Cannot get episode identifiers without imdb_id or tmdb_id.")
        return set()

    conn = get_db_connection()
    # Select only distinct season and episode numbers
    query = '''
        SELECT DISTINCT season_number, episode_number
        FROM media_items
        WHERE type = 'episode'
    '''
    params = []

    # Build query based on available ID (prioritize imdb_id)
    id_field, id_value = ('imdb_id', imdb_id) if imdb_id else ('tmdb_id', tmdb_id)
    query += f' AND {id_field} = ?'
    params.append(id_value)

    # Add filtering for non-null season/episode numbers
    query += ' AND season_number IS NOT NULL AND episode_number IS NOT NULL'

    identifiers = set()
    try:
        cursor = conn.execute(query, params)
        for row in cursor:
             s_num, e_num = row['season_number'], row['episode_number']
             # Basic check for None again, although IS NOT NULL should prevent it
             if s_num is not None and e_num is not None:
                 try:
                     # Ensure they are integers before adding to the set
                     identifiers.add((int(s_num), int(e_num)))
                 except (ValueError, TypeError):
                      logging.warning(f"Skipping invalid non-integer season/episode number pair ({s_num}, {e_num}) from DB for show {id_field}={id_value}")
             # No need for else, IS NOT NULL handles it

        logging.debug(f"Found {len(identifiers)} unique S/E pairs in DB for {id_field}={id_value}")
        return identifiers
    except Exception as e:
        logging.error(f"Error retrieving episode identifiers for show {id_field}={id_value}: {str(e)}")
        logging.debug(f"Query: {query}, Params: {params}")
        return set() # Return empty set on error
    finally:
        conn.close()

def get_media_item_ids(imdb_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    Efficiently retrieves state and episode identifiers for a list of IMDb IDs.

    Args:
        imdb_ids: A list of IMDb IDs to query.

    Returns:
        A dictionary where keys are the input IMDb IDs and values are dictionaries
        containing:
        - 'movie_state': State of the movie item ('Collected', 'Wanted', etc.), or None if no movie found.
        - 'episode_identifiers': A set of (season_number, episode_number) tuples for existing episodes.
        - 'has_requested': Boolean indicating if any episode for this show has requested_season=TRUE.
    """
    if not imdb_ids:
        return {}

    conn = get_db_connection()
    results = {
        imdb_id: {
            "movie_state": None,
            "episode_identifiers": set(),
            "has_requested": False
        }
        for imdb_id in imdb_ids
    }

    try:
        placeholders = ','.join('?' * len(imdb_ids))
        query = f'''
            SELECT
                imdb_id,
                type,
                state,
                season_number,
                episode_number,
                requested_season
            FROM media_items
            WHERE imdb_id IN ({placeholders})
        '''
        
        cursor = conn.execute(query, imdb_ids)
        rows = cursor.fetchall()

        for row in rows:
            imdb_id_from_row = row['imdb_id']
            # Ensure the imdb_id from the row is one we requested
            if imdb_id_from_row in results:
                item_type = row['type']
                state = row['state']
                
                if item_type == 'movie':
                    # Store the movie state (last one wins if multiple movie rows exist for same ID, though unlikely)
                    results[imdb_id_from_row]['movie_state'] = state
                elif item_type == 'episode':
                    season_num = row['season_number']
                    episode_num = row['episode_number']
                    requested = row['requested_season']
                    
                    # Add episode identifier if valid
                    if season_num is not None and episode_num is not None:
                        try:
                            results[imdb_id_from_row]['episode_identifiers'].add(
                                (int(season_num), int(episode_num))
                            )
                        except (ValueError, TypeError):
                            logging.warning(f"Skipping invalid non-integer season/episode number pair ({season_num}, {episode_num}) from DB for IMDb {imdb_id_from_row}")
                    
                    # Update has_requested flag if True
                    if requested: # SQLite stores BOOLEAN as 0 or 1
                         results[imdb_id_from_row]['has_requested'] = True

        logging.info(f"Retrieved DB states/identifiers for {len(rows)} rows corresponding to {len(imdb_ids)} requested IMDb IDs.")
        return results

    except Exception as e:
        logging.error(f"Error in get_media_item_ids: {str(e)}")
        logging.debug(f"Query: {query}, Params: {imdb_ids}")
        # Return the initialized dict, possibly partially filled or empty on error
        return results
    finally:
        if conn:
            conn.close()

def get_item_count_by_state(state: str) -> int:
    """Get the total count of items in a specific state."""
    conn = None
    try:
        conn = get_db_connection()
        query = 'SELECT COUNT(*) as count FROM media_items WHERE state = ?'
        cursor = conn.execute(query, (state,))
        result = cursor.fetchone()
        return result['count'] if result else 0
    except Exception as e:
        logging.error(f"Error getting item count for state '{state}': {e}")
        return 0
    finally:
        if conn:
            conn.close()

# Define __all__ for explicit exports
__all__ = [
    'search_movies', 
    'search_tv_shows', 
    'get_all_media_items', 
    'get_media_item_presence', 
    'get_media_item_by_id',
    'get_movie_runtime', 
    'get_episode_runtime', 
    'get_episode_count',
    'get_all_season_episode_counts',
    'row_to_dict',
    'get_all_videos',
    'get_video_by_id',
    'get_media_country_code',
    'get_episode_details',
    'get_imdb_aliases',
    'get_media_items_by_ids_batch',
    'get_media_item_by_filename',
    'check_existing_media_item',
    'get_wake_count',
    'get_show_episode_identifiers_from_db',
    'get_media_item_ids',
    'get_item_count_by_state'
]