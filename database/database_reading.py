from .core import get_db_connection
import logging
import os

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

def get_all_media_items(state=None, media_type=None, tmdb_id=None):
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
    if tmdb_id:
        query += ' AND tmdb_id = ?'
        params.append(tmdb_id)
    cursor = conn.execute(query, params)
    items = cursor.fetchall()
    conn.close()
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
    Retrieve all videos from the database that have associated files.
    """
    conn = get_db_connection()
    try:
        cursor = conn.execute('''
            SELECT id, title, filled_by_file
            FROM media_items
            WHERE filled_by_file IS NOT NULL
        ''')
        videos = [row_to_dict(row) for row in cursor]
        # Construct full file path for each video
        for video in videos:
            if video['filled_by_file']:
                base_name = video['filled_by_file']
                video['filled_by_file'] = os.path.join(r'/mnt/zurg/movies', base_name, base_name)
        return videos
    finally:
        conn.close()

def get_video_by_id(video_id):
    """
    Retrieve a specific video by its ID.
    """
    conn = get_db_connection()
    try:
        cursor = conn.execute('''
            SELECT id, title, filled_by_file, state
            FROM media_items
            WHERE id = ?
        ''', (video_id,))
        row = cursor.fetchone()
        if row:
            video = row_to_dict(row)
            logging.info(f"Found video in database: {video}")
            if video['filled_by_file']:
                base_name = video['filled_by_file']
                video['filled_by_file'] = os.path.join(r'/mnt/zurg/movies', base_name, base_name)
                logging.info(f"Constructed file path: {video['filled_by_file']}")
            return video
        logging.error(f"No video found with ID {video_id}")
        return None
    finally:
        conn.close()