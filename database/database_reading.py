from .core import get_db_connection
import logging
import os
import json
from typing import List, Dict, Optional, Tuple, Set, Any
import functools
import unicodedata
try:
    import tracemalloc
    tracemalloc_available = True
except ImportError:
    tracemalloc_available = False
    tracemalloc = None # Define tracemalloc as None if import failed
import time
from utilities.settings import get_setting

def normalize_string_for_comparison(text: str) -> str:
    """
    Normalize a string for reliable comparison, especially with Unicode characters.
    Handles Cyrillic and other non-ASCII characters properly.
    
    Args:
        text: The string to normalize
        
    Returns:
        Normalized string suitable for database comparison
    """
    if not text:
        return text
    
    # Normalize Unicode to NFC form (canonical composition)
    # This ensures consistent representation of characters with diacritics/accents
    normalized = unicodedata.normalize('NFC', text)
    
    # Convert to lowercase for case-insensitive comparison
    return normalized.lower()

def trace_memory_usage(func):
    """
    A decorator to trace and log memory usage of a function using tracemalloc.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        tracemalloc_enabled = get_setting('Debug', 'enable_tracemalloc', False)
        mem_before, peak_before = 0, 0
        start_time = time.time()

        # Check if tracing is available, enabled, and active
        if tracemalloc_available and tracemalloc_enabled:
            if tracemalloc.is_tracing():
                try:
                    mem_before, peak_before = tracemalloc.get_traced_memory()
                except Exception as e_mem:
                    logging.error(f"[Tracemalloc DB] Error getting memory before {func.__name__}: {e_mem}")
                    tracemalloc_enabled = False
            else:
                tracemalloc_enabled = False
        
        result = func(*args, **kwargs)
        
        duration = time.time() - start_time
        
        # Log memory usage if tracing was active for this call
        if tracemalloc_available and tracemalloc_enabled and tracemalloc.is_tracing():
            try:
                mem_after, peak_after = tracemalloc.get_traced_memory()
                mem_delta = mem_after - mem_before
                peak_delta = peak_after - peak_before

                items_count = 'N/A'
                if isinstance(result, (list, dict)):
                    items_count = len(result)

                log_level = logging.INFO if abs(mem_delta / (1024*1024)) < 5 else logging.WARNING
                logging.log(log_level,
                            f"[Tracemalloc DB] {func.__name__} completed in {duration:.3f}s. "
                            f"Returned Items: {items_count}. "
                            f"Mem Delta: {mem_delta / (1024*1024):+.2f}MB ({mem_before / (1024*1024):.2f} -> {mem_after / (1024*1024):.2f}MB). "
                            f"Peak Delta During Call: {peak_delta / (1024*1024):+.2f}MB.")
            except Exception as e_mem:
                logging.error(f"[Tracemalloc DB] Error getting memory after {func.__name__}: {e_mem}")
        
        return result
    return wrapper

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

@trace_memory_usage
def get_all_media_items(state=None, media_type=None, imdb_id=None, tmdb_id=None, limit: Optional[int] = None):
    """
    Retrieves all media items matching the criteria and returns them as a list.
    This function is a wrapper around stream_all_media_items for backward compatibility.
    It can be memory-intensive for large datasets.
    """
    return list(stream_all_media_items(state, media_type, imdb_id, tmdb_id, limit))

@trace_memory_usage
def stream_all_media_items(state=None, media_type=None, imdb_id=None, tmdb_id=None, limit: Optional[int] = None):
    """
    A generator that streams media items from the database one by one.
    This is memory-efficient and suitable for large datasets.
    """
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
        
        # Yield rows one by one instead of fetching all at once
        for row in cursor:
            yield dict(row)

    except Exception as e:
        logging.error(f"Error executing stream_all_media_items query: {e}")
        # To aid debugging, it's good practice to log the query that failed
        try:
            logging.debug(f"Failed Query: {query}, Params: {params}")
        except NameError:
            logging.debug("Query and params were not available at the time of error.")
        # An empty generator will be returned implicitly on error after this
    finally:
        if conn:
            conn.close()

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
    query_start_time = None # Initialize
    try:
        query = '''
            SELECT AVG(runtime) as runtime FROM media_items 
            WHERE tmdb_id = ? AND type = "episode"
        '''
        query_start_time = time.time()
        cursor = conn.execute(query, (tmdb_id,))
        result = cursor.fetchone()
        query_duration = time.time() - query_start_time
        #logging.debug(f"get_episode_runtime query for TMDB ID {tmdb_id} took {query_duration:.4f}s")
        return result['runtime'] if result and result['runtime'] is not None else None
    except Exception as e:
        if query_start_time: # Log duration even if fetchone fails
            query_duration = time.time() - query_start_time
            #logging.debug(f"get_episode_runtime query (failed) for TMDB ID {tmdb_id} took {query_duration:.4f}s before error")
        logging.error(f"Error retrieving episode runtime (TMDB ID: {tmdb_id}): {str(e)}")
        return None
    finally:
        if conn:
            conn.close()

def get_episode_count(tmdb_id):
    conn = get_db_connection()
    query_start_time = None # Initialize
    try:
        query = '''
            SELECT COUNT(*) as episode_count 
            FROM (
                SELECT DISTINCT season_number, episode_number, version
                FROM media_items 
                WHERE tmdb_id = ? AND type = "episode"
            )
        '''
        query_start_time = time.time()
        cursor = conn.execute(query, (tmdb_id,))
        result = cursor.fetchone()
        query_duration = time.time() - query_start_time
        #logging.debug(f"get_episode_count query for TMDB ID {tmdb_id} took {query_duration:.4f}s")
        return result['episode_count'] if result else 0
    except Exception as e:
        if query_start_time: # Log duration even if fetchone fails
            query_duration = time.time() - query_start_time
            #logging.debug(f"get_episode_count query (failed) for TMDB ID {tmdb_id} took {query_duration:.4f}s before error")
        logging.error(f"Error retrieving episode count (TMDB ID: {tmdb_id}): {str(e)}")
        return 0
    finally:
        if conn:
            conn.close()

def get_all_season_episode_counts(tmdb_id):
    conn = get_db_connection()
    try:
        query = '''
            SELECT season_number, COUNT(DISTINCT episode_number) as episode_count
            FROM media_items
            WHERE tmdb_id = ? AND type = "episode"
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

@trace_memory_usage
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
        - 'collected_movie_qualities': A set of version strings for collected movies (e.g., {'1080p', '2160p'}).
        - 'episode_identifiers': A set of (season_number, episode_number) tuples for existing episodes.
        - 'has_requested': Boolean indicating if any episode for this show has requested_season=TRUE.
    """
    if not imdb_ids:
        return {}

    conn = get_db_connection()
    results = {
        imdb_id: {
            "movie_state": None,
            "collected_movie_qualities": set(),
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
                requested_season,
                version 
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
                version = row['version']
                
                if item_type == 'movie':
                    # Store the movie state (last one wins if multiple movie rows exist for same ID, though unlikely)
                    results[imdb_id_from_row]['movie_state'] = state
                    if state == 'Collected' and version:
                        results[imdb_id_from_row]['collected_movie_qualities'].add(version)
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

def check_item_exists_by_directory_name(item_directory_name: str) -> bool:
    """
    Check if any media item exists in the database where the item_directory_name
    matches either the 'filled_by_title' or 'real_debrid_original_title' fields.
    Uses Unicode normalization for proper handling of Cyrillic and other non-ASCII characters.

    Args:
        item_directory_name: The directory name component to check against specific title fields.
                             Example: 'Movie Name (Year)'

    Returns:
        True if an item matching the name is found in either specified field, False otherwise.
    """
    conn = None
    try:
        conn = get_db_connection()
        
        # Normalize the search term for Unicode comparison
        normalized_search = normalize_string_for_comparison(item_directory_name)
        
        # Fetch potential matches - cast fields to avoid None comparisons
        query = '''
            SELECT filled_by_title, real_debrid_original_title
            FROM media_items 
            WHERE filled_by_title IS NOT NULL OR real_debrid_original_title IS NOT NULL
        '''
        
        cursor = conn.execute(query)
        rows = cursor.fetchall()
        
        # Check each row with proper Unicode normalization
        for row in rows:
            filled_by_title = row['filled_by_title']
            real_debrid_title = row['real_debrid_original_title']
            
            if filled_by_title and normalize_string_for_comparison(filled_by_title) == normalized_search:
                logging.debug(f"Found existing item in DB with matching filled_by_title: {item_directory_name}")
                return True
                
            if real_debrid_title and normalize_string_for_comparison(real_debrid_title) == normalized_search:
                logging.debug(f"Found existing item in DB with matching real_debrid_original_title: {item_directory_name}")
                return True
        
        logging.debug(f"No existing items found in DB with matching directory/title name: {item_directory_name}")
        return False
        
    except Exception as e:
        logging.error(f"Error checking items by directory/title name ('{item_directory_name}'): {str(e)}")
        return False # Assume not found on error to avoid blocking unnecessarily
    finally:
        if conn:
            conn.close()

def check_item_exists_by_symlink_path(original_dir_path: str) -> bool:
    """
    Check if any media item exists in the database where the 'original_path_for_symlink' 
    starts with the provided directory path. This is used to check if any file *within* 
    the specified directory is already tracked.
    Uses Unicode normalization for proper handling of Cyrillic and other non-ASCII characters.

    Args:
        original_dir_path: The full directory path to check against the beginning of 
                           the 'original_path_for_symlink' field.
                           Example: '/mnt/zurg/shows/Show.Name.S01E01'

    Returns:
        True if an item whose path starts with the directory path is found, False otherwise.
    """
    conn = None
    try:
        conn = get_db_connection()
        
        # We need to check if original_path_for_symlink starts with the directory path + separator
        # Ensure the directory path ends with a separator for the query
        path_prefix = original_dir_path.rstrip(os.path.sep) + os.path.sep
        normalized_prefix = normalize_string_for_comparison(path_prefix)
        
        # Fetch all non-null original_path_for_symlink values
        query = '''
            SELECT original_path_for_symlink
            FROM media_items 
            WHERE original_path_for_symlink IS NOT NULL
        '''
        
        cursor = conn.execute(query)
        rows = cursor.fetchall()
        
        # Check each path with proper Unicode normalization
        for row in rows:
            original_path = row['original_path_for_symlink']
            if original_path:
                normalized_path = normalize_string_for_comparison(original_path)
                if normalized_path.startswith(normalized_prefix):
                    logging.debug(f"Found existing item in DB whose original_path_for_symlink starts with: {path_prefix}")
                    return True
        
        logging.debug(f"No existing item found in DB whose original_path_for_symlink starts with: {path_prefix}")
        return False
        
    except Exception as e:
        logging.error(f"Error checking items by original_path_for_symlink prefix ('{original_dir_path}'): {str(e)}")
        return False # Assume not found on error to avoid blocking unnecessarily
    finally:
        if conn:
            conn.close()

def check_item_exists_with_symlink_path_containing(path_segment: str) -> bool:
    """
    Check if any media item exists in the database where the 'original_path_for_symlink'
    contains the provided path_segment.
    Uses Unicode normalization for proper handling of Cyrillic and other non-ASCII characters.

    Args:
        path_segment: The string segment to search for within the 'original_path_for_symlink' field.
                      Example: 'Movie.Name.Year' or a specific filename.

    Returns:
        True if an item whose original_path_for_symlink contains the segment is found, False otherwise.
    """
    conn = None
    try:
        conn = get_db_connection()
        
        # Normalize the search segment for Unicode comparison
        normalized_segment = normalize_string_for_comparison(path_segment)
        
        # Fetch all non-null original_path_for_symlink values
        query = '''
            SELECT original_path_for_symlink
            FROM media_items 
            WHERE original_path_for_symlink IS NOT NULL
        '''
        
        cursor = conn.execute(query)
        rows = cursor.fetchall()
        
        # Check each path with proper Unicode normalization
        for row in rows:
            original_path = row['original_path_for_symlink']
            if original_path:
                normalized_path = normalize_string_for_comparison(original_path)
                if normalized_segment in normalized_path:
                    logging.debug(f"Found existing item in DB where original_path_for_symlink CONTAINS segment: {path_segment}")
                    return True
        
        logging.debug(f"No existing items found in DB where original_path_for_symlink CONTAINS segment: {path_segment}")
        return False
        
    except Exception as e:
        logging.error(f"Error checking items by original_path_for_symlink containing segment ('{path_segment}'): {str(e)}")
        return False # Assume not found on error
    finally:
        if conn:
            conn.close()

def get_distinct_library_shows(letter: Optional[str] = None) -> List[Dict[str, str]]:
    """
    Retrieves a list of distinct shows from the media_items table, optionally filtered by the starting letter of the title.
    A show is identified by a unique IMDb ID. The representative title is chosen by prioritizing
    'Collected' items first, then alphabetically.

    Args:
        letter: Optional. If provided, filters shows whose titles start with this letter.
                If '#', filters for titles not starting with A-Z. Case-insensitive for letters.

    Returns:
        List of dictionaries, each with 'imdb_id' and 'title'.
    """
    conn = get_db_connection()
    shows = []
    params = []
    try:
        # CTE to select a single representative title for each imdb_id
        # It prioritizes titles from 'Collected' episodes, then alphabetically.
        base_query = """
        WITH ShowRepresentativeTitles AS (
            SELECT
                imdb_id,
                title,
                ROW_NUMBER() OVER (PARTITION BY imdb_id ORDER BY CASE state WHEN 'Collected' THEN 0 ELSE 1 END, title COLLATE NOCASE ASC) as rn
            FROM media_items
            WHERE type = 'episode' AND imdb_id IS NOT NULL AND imdb_id != ''
        ),
        RankedShowTitles AS (
            SELECT
                imdb_id,
                title
            FROM ShowRepresentativeTitles
            WHERE rn = 1
        ),
        ShowCollectionStatus AS (
            SELECT
                imdb_id,
                MAX(CASE WHEN state = 'Collected' THEN 1 ELSE 0 END) as has_collected_episode
            FROM media_items
            WHERE type = 'episode' AND imdb_id IS NOT NULL AND imdb_id != ''
            GROUP BY imdb_id
        )
        SELECT
            rst.imdb_id,
            rst.title
        FROM RankedShowTitles rst
        LEFT JOIN ShowCollectionStatus scs ON rst.imdb_id = scs.imdb_id -- Use LEFT JOIN in case some shows have no status entries (though unlikely)
        WHERE 1=1 
        """
        
        filter_conditions = []
        # Apply letter filter to the selected representative title (rst.title)
        if letter:
            letter_upper = letter.upper()
            if letter_upper == '#':
                filter_conditions.append("NOT (SUBSTR(UPPER(rst.title), 1, 1) BETWEEN 'A' AND 'Z')")
            elif 'A' <= letter_upper <= 'Z':
                filter_conditions.append("UPPER(rst.title) LIKE ?")
                params.append(letter_upper + '%')
            else:
                logging.warning(f"Invalid letter '{letter}' provided for filtering shows. Returning all shows or based on other criteria.")
        
        if filter_conditions:
            base_query += " AND " + " AND ".join(filter_conditions)
        
        # Order by collection status (shows with collected episodes first), then by title
        base_query += " ORDER BY COALESCE(scs.has_collected_episode, 0) DESC, rst.title COLLATE NOCASE ASC;"

        cursor = conn.execute(base_query, params)
        for row in cursor:
            if row['imdb_id'] and row['title']:
                shows.append({'imdb_id': row['imdb_id'], 'title': row['title']})
        
        logging.debug(f"Found {len(shows)} distinct shows in the library (filter: '{letter}', new query).")
        return shows
    except Exception as e:
        logging.error(f"Error retrieving distinct library shows (filter: '{letter}', new query): {str(e)}")
        return [] 
    finally:
        if conn:
            conn.close()

def get_collected_episodes_count(imdb_id: str, version_name: str) -> int:
    """
    Counts the number of distinct collected episodes for a given show (IMDb ID) and version.
    Args:
        imdb_id: The IMDb ID of the show.
        version_name: The specific version (e.g., '1080p', '2160p').
    Returns:
        The count of collected episodes.
    """
    conn = get_db_connection()
    try:
        # Count distinct episodes based on season and episode number for a given imdb_id and version
        # Remove asterisks from the 'version' column before comparison.
        query = """
            SELECT COUNT(DISTINCT season_number || '-' || episode_number) as count
            FROM media_items
            WHERE imdb_id = ? 
              AND LOWER(REPLACE(version, '*', '')) = LOWER(?) 
              AND type = 'episode' 
              AND state = 'Collected'
              AND season_number IS NOT NULL
              AND episode_number IS NOT NULL;
        """
        params = (imdb_id, version_name)
        cursor = conn.execute(query, params)
        result = cursor.fetchone()
        count = result['count'] if result and result['count'] is not None else 0
        logging.debug(f"DB Collected Count for IMDb: {imdb_id}, Version: {version_name} (after asterisk trim) -> {count}")
        return count
    except Exception as e:
        logging.error(f"Error counting collected episodes for IMDb ID {imdb_id} (version: {version_name}): {str(e)}")
        return 0
    finally:
        if conn:
            conn.close()

def get_collected_episode_numbers(imdb_id: str, version_name: str) -> Set[Tuple[int, int]]:
    """
    Retrieves a set of (season_number, episode_number) tuples for a given IMDb ID and version name
    from the media_items table.
    """
    conn = get_db_connection()
    collected_episodes = set()
    try:
        # Select distinct season and episode numbers for a given imdb_id and version
        # This logic mirrors get_collected_episodes_count
        query = """
            SELECT DISTINCT season_number, episode_number
            FROM media_items
            WHERE imdb_id = ?
              AND LOWER(REPLACE(version, '*', '')) = LOWER(?)
              AND type = 'episode'
              AND state = 'Collected'
              AND season_number IS NOT NULL
              AND episode_number IS NOT NULL;
        """
        params = (imdb_id, version_name)
        cursor = conn.execute(query, params)
        for row in cursor.fetchall():
            try:
                # Ensure we are adding integers to the set
                s_num = int(row['season_number'])
                e_num = int(row['episode_number'])
                collected_episodes.add((s_num, e_num))
            except (ValueError, TypeError):
                logging.warning(f"Skipping invalid non-integer season/episode number pair ({row['season_number']}, {row['episode_number']}) from DB for show {imdb_id}")

        return collected_episodes
    except Exception as e:
        logging.error(f"Error getting collected episode numbers for {imdb_id} (version: {version_name}): {e}", exc_info=True)
        return set()
    finally:
        if conn:
            conn.close()

def get_media_item_presence_overall(imdb_id: str | None = None, tmdb_id: str | None = None) -> str:
    """Return an aggregated presence state for a title.

    Logic priority:
        1. Any row Blacklisted  -> "Blacklisted"
        2. Mixture that contains Collected and something else -> "Partial"
        3. All rows Collected -> "Collected"
        4. Otherwise return the first state found (Wanted, Upgrading, etc.)
        5. If no rows found -> "Missing"
    """
    conn = get_db_connection()
    try:
        if imdb_id is not None:
            id_field, id_value = 'imdb_id', imdb_id
        elif tmdb_id is not None:
            id_field, id_value = 'tmdb_id', tmdb_id
        else:
            raise ValueError("Either imdb_id or tmdb_id must be provided.")

        query = f'SELECT DISTINCT state FROM media_items WHERE {id_field} = ?'
        cursor = conn.execute(query, (id_value,))
        states = {row[0] for row in cursor.fetchall()}

        if not states:
            return "Missing"

        if 'Blacklisted' in states:
            return 'Blacklisted'

        if 'Collected' in states:
            return 'Collected' if len(states) == 1 else 'Partial'

        return next(iter(states))
    except Exception as e:
        logging.error(f"Error retrieving aggregated media item status: {e}")
        return 'Missing'
    finally:
        conn.close()

def is_any_file_in_db_for_item(imdb_id: str, filenames: List[str]) -> bool:
    """
    Check if any of the provided filenames match a 'filled_by_file' entry
    for a specific IMDb ID in the database. Comparison is done on base filenames.

    Args:
        imdb_id: The IMDb ID of the media item.
        filenames: A list of filenames from the torrent to check.

    Returns:
        True if a matching file is found, False otherwise.
    """
    if not imdb_id or not filenames:
        return False

    conn = get_db_connection()
    try:
        # Get all 'filled_by_file' for the given imdb_id
        cursor = conn.execute(
            "SELECT filled_by_file FROM media_items WHERE imdb_id = ? AND filled_by_file IS NOT NULL",
            (imdb_id,)
        )
        db_files = cursor.fetchall()

        if not db_files:
            return False

        # Create a set of base filenames from the database for efficient lookup
        db_basenames = {os.path.basename(row['filled_by_file']) for row in db_files}

        # Check if any of the torrent's filenames match
        for f in filenames:
            basename = os.path.basename(f)
            if basename in db_basenames:
                logging.info(f"Found matching file in DB for IMDb ID {imdb_id}: {basename}")
                return True

        return False
    except Exception as e:
        logging.error(f"Error checking if file is in DB for IMDb ID {imdb_id}: {e}")
        return False
    finally:
        if conn:
            conn.close()

@trace_memory_usage
def get_distinct_imdb_ids(states: Optional[List[str]] = None, media_type: Optional[str] = None) -> List[str]:
    """Return a list of distinct imdb_id values matching the supplied filters.

    This is a lightweight alternative to ``get_all_media_items`` when you only
    need the unique show/movie identifiers and not the full row payload.  It
    dramatically reduces I/O and memory usage for large tables because it
    leverages ``SELECT DISTINCT imdb_id`` directly in SQL and only transfers a
    single column per row.

    Args:
        states: Optional list of states (e.g. ["Wanted", "Collected"]. If
            ``None`` all states are considered.
        media_type: Optional media type filter ("movie" or "episode"). If
            ``None`` all types are considered.

    Returns:
        A list of imdb_id strings. Rows with NULL/empty imdb_id are ignored.
    """
    conn = None
    try:
        conn = get_db_connection()
        query = "SELECT DISTINCT imdb_id FROM media_items WHERE imdb_id IS NOT NULL AND imdb_id != ''"
        params: List[Any] = []

        # State filtering
        if states:
            placeholders = ",".join(["?" for _ in states])
            query += f" AND state IN ({placeholders})"
            params.extend(states)

        # Media type filtering
        if media_type:
            query += " AND type = ?"
            params.append(media_type)

        cursor = conn.execute(query, params)
        return [row["imdb_id"] for row in cursor.fetchall()]
    except Exception as e:
        logging.error(f"Error retrieving distinct imdb_ids: {e}")
        return []
    finally:
        if conn:
            conn.close()

# Define __all__ for explicit exports
__all__ = [
    'normalize_string_for_comparison',
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
    'get_item_count_by_state',
    'check_item_exists_by_directory_name',
    'check_item_exists_by_symlink_path',
    'check_item_exists_with_symlink_path_containing',
    'get_distinct_library_shows',
    'get_collected_episodes_count',
    'get_collected_episode_numbers',
    'get_media_item_presence_overall',
    'get_distinct_imdb_ids',
    'is_any_file_in_db_for_item'
]