import logging
from .core import get_db_connection, normalize_string, get_existing_airtime
from manual_blacklist import is_blacklisted
from typing import List, Dict, Any
import json
from datetime import datetime
from metadata.metadata import get_tmdb_id_and_media_type
import random

def add_wanted_items(media_items_batch: List[Dict[str, Any]], versions_input):
    from metadata.metadata import get_show_airtime_by_imdb_id

    conn = get_db_connection()
    try:
        items_added = 0
        items_updated = 0
        items_skipped = 0
        airtime_cache = {}

        # Handle different types of versions input
        if isinstance(versions_input, str):
            try:
                versions = json.loads(versions_input)
            except json.JSONDecodeError:
                logging.error(f"Invalid JSON string for versions: {versions_input}")
                versions = {}
        elif isinstance(versions_input, list):
            versions = {version: True for version in versions_input}
        else:
            versions = versions_input

        # ----------------------------
        # Initial Purge Implementation
        # ----------------------------

        # Collect identifiers for movies and episodes
        movie_ids = set()
        episode_ids = set()

        for item in media_items_batch:
            imdb_id = item.get('imdb_id')
            tmdb_id = item.get('tmdb_id')
            item_type = 'episode' if 'season_number' in item and 'episode_number' in item else 'movie'

            if item_type == 'movie':
                if imdb_id:
                    movie_ids.add(('imdb_id', imdb_id))
                elif tmdb_id:
                    movie_ids.add(('tmdb_id', tmdb_id))
            else:
                season_number = item.get('season_number')
                episode_number = item.get('episode_number')
                if imdb_id:
                    episode_ids.add(('imdb_id', imdb_id, season_number, episode_number))
                elif tmdb_id:
                    episode_ids.add(('tmdb_id', tmdb_id, season_number, episode_number))

        # Fetch existing movies from the database
        existing_movie_ids = set()
        if movie_ids:
            id_types = {'imdb_id': [], 'tmdb_id': []}
            for id_type, id_value in movie_ids:
                id_types[id_type].append(id_value)
            
            for id_type, id_values in id_types.items():
                # Process in chunks of 500 to avoid SQLite limitations
                chunk_size = 500
                for i in range(0, len(id_values), chunk_size):
                    chunk = id_values[i:i+chunk_size]
                    placeholders = ', '.join(['?'] * len(chunk))
                    query = f'''
                        SELECT imdb_id, tmdb_id FROM media_items
                        WHERE type = 'movie' AND {id_type} IN ({placeholders})
                    '''
                    cursor = conn.execute(query, chunk)
                    for row in cursor:
                        if row['imdb_id']:
                            existing_movie_ids.add(row['imdb_id'])
                        if row['tmdb_id']:
                            existing_movie_ids.add(row['tmdb_id'])
                    cursor.close()

        # Fetch existing episodes from the database
        existing_episode_ids = set()
        if episode_ids:
            # Process in chunks of 500 to avoid SQLite limitations
            chunk_size = 500
            episode_id_list = list(episode_ids)
            for i in range(0, len(episode_id_list), chunk_size):
                chunk = episode_id_list[i:i+chunk_size]
                conditions = []
                params = []
                for id_type, id_value, season_number, episode_number in chunk:
                    conditions.append(f"(type = 'episode' AND {id_type} = ? AND season_number = ? AND episode_number = ?)")
                    params.extend([id_value, season_number, episode_number])
                
                placeholders = ' OR '.join(conditions)
                query = f'''
                    SELECT imdb_id, tmdb_id, season_number, episode_number FROM media_items
                    WHERE ''' + placeholders
                cursor = conn.execute(query, params)
                for row in cursor:
                    id_value = row['imdb_id'] or row['tmdb_id']
                    existing_episode_ids.add((id_value, row['season_number'], row['episode_number']))
                cursor.close()

        # Filter out existing items from media_items_batch
        filtered_media_items_batch = []
        for item in media_items_batch:
            imdb_id = item.get('imdb_id')
            tmdb_id = item.get('tmdb_id')
            item_type = 'episode' if 'season_number' in item and 'episode_number' in item else 'movie'

            if item_type == 'movie':
                if (imdb_id and imdb_id in existing_movie_ids) or (tmdb_id and tmdb_id in existing_movie_ids):
                    items_skipped += 1
                    logging.debug(f"Skipping existing movie: {item.get('title', 'Unknown')} (IMDb ID: {imdb_id}, TMDb ID: {tmdb_id})")
                    continue
            else:
                season_number = item.get('season_number')
                episode_number = item.get('episode_number')
                id_value = imdb_id or tmdb_id
                if (id_value, season_number, episode_number) in existing_episode_ids:
                    items_skipped += 1
                    logging.debug(f"Skipping existing episode: {item.get('title', 'Unknown')} S{season_number}E{episode_number} (ID: {id_value})")
                    continue

            filtered_media_items_batch.append(item)

        # Update the media_items_batch to the filtered list
        media_items_batch = filtered_media_items_batch

        # ----------------------------
        # End of Initial Purge
        # ----------------------------

        for item in media_items_batch:
            if not item.get('imdb_id'):
                logging.warning(f"Skipping item without valid IMDb ID: {item.get('title', 'Unknown')}")
                items_skipped += 1
                continue

            if is_blacklisted(item['imdb_id']):
                logging.debug(f"Skipping blacklisted item: {item.get('title', 'Unknown')} (IMDb ID: {item['imdb_id']})")
                items_skipped += 1
                continue

            if not item.get('tmdb_id'):
                tmdb_id, media_type = get_tmdb_id_and_media_type(item['imdb_id'])
                if tmdb_id:
                    item['tmdb_id'] = tmdb_id
                else:
                    logging.warning(f"Unable to retrieve tmdb_id for {item.get('title', 'Unknown')} (IMDb ID: {item['imdb_id']})")

            normalized_title = normalize_string(str(item.get('title', 'Unknown')))
            item_type = 'episode' if 'season_number' in item and 'episode_number' in item else 'movie'

            genres = json.dumps(item.get('genres', []))  # Convert genres list to JSON string

            for version, enabled in versions.items():
                if not enabled:
                    continue

                # Check if item exists for this version
                if item_type == 'movie':
                    cursor = conn.execute('''
                        SELECT id, release_date, state FROM media_items
                        WHERE (imdb_id = ? OR tmdb_id = ?) AND type = 'movie' AND version = ?
                    ''', (item.get('imdb_id'), item.get('tmdb_id'), version))
                    existing_item = cursor.fetchone()
                    cursor.close()
                else:
                    cursor = conn.execute('''
                        SELECT id, release_date, state FROM media_items
                        WHERE (imdb_id = ? OR tmdb_id = ?) AND type = 'episode' AND season_number = ? AND episode_number = ? AND version = ?
                    ''', (item.get('imdb_id'), item.get('tmdb_id'), item['season_number'], item['episode_number'], version))
                    existing_item = cursor.fetchone()
                    cursor.close()

                if existing_item:
                    # Item exists, check if we need to update
                    if existing_item['state'] == 'Blacklisted':
                        logging.debug(f"Skipping update for blacklisted item: {normalized_title} (Version: {version})")
                        items_skipped += 1
                    elif existing_item['state'] != 'Collected':
                        if existing_item['release_date'] != item.get('release_date'):
                            conn.execute('''
                                UPDATE media_items
                                SET release_date = ?, last_updated = ?, state = ?
                                WHERE id = ?
                            ''', (item.get('release_date'), datetime.now(), 'Wanted', existing_item['id']))
                            logging.debug(f"Updated release date for existing item: {normalized_title} (Version: {version})")
                            items_updated += 1
                        else:
                            logging.debug(f"Skipping update for existing item: {normalized_title} (Version: {version})")
                            items_skipped += 1
                    else:
                        logging.debug(f"Skipping update for collected item: {normalized_title} (Version: {version})")
                        items_skipped += 1
                else:
                    # Insert new item
                    if item_type == 'movie':
                        conn.execute('''
                            INSERT INTO media_items
                            (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated, version, genres, runtime)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            item.get('imdb_id'), item.get('tmdb_id'), normalized_title, item.get('year'),
                            item.get('release_date'), 'Wanted', 'movie', datetime.now(), version, genres, item.get('runtime')
                        ))
                    else:
                        # For episodes, get the airtime
                        if item['imdb_id'] not in airtime_cache:
                            airtime_cache[item['imdb_id']] = get_existing_airtime(conn, item['imdb_id'])
                            if airtime_cache[item['imdb_id']] is None:
                                logging.debug(f"No existing airtime found for show {item['imdb_id']}, fetching from metadata")
                                airtime_cache[item['imdb_id']] = get_show_airtime_by_imdb_id(item['imdb_id'])

                            # Ensure we always have a default airtime
                            if not airtime_cache[item['imdb_id']]:
                                airtime_cache[item['imdb_id']] = '19:00'
                                logging.debug(f"No airtime found, defaulting to 19:00 for show {item['imdb_id']}")

                            logging.debug(f"Airtime for show {item['imdb_id']} set to {airtime_cache[item['imdb_id']]}")

                        airtime = airtime_cache[item['imdb_id']]

                        conn.execute('''
                            INSERT INTO media_items
                            (imdb_id, tmdb_id, title, year, release_date, state, type, season_number, episode_number, episode_title, last_updated, version, runtime, airtime, genres)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            item.get('imdb_id'), item.get('tmdb_id'), normalized_title, item.get('year'),
                            item.get('release_date'), 'Wanted', 'episode',
                            item.get('season_number'), item['episode_number'], item.get('episode_title', ''),
                            datetime.now(), version, item.get('runtime'), airtime, genres
                        ))
                    logging.debug(f"Adding new {item_type} as Wanted in DB: {normalized_title} (Version: {version})")
                    items_added += 1

        conn.commit()
        logging.debug(f"Wanted items processing complete. Added: {items_added}, Updated: {items_updated}, Skipped: {items_skipped}")
    except Exception as e:
        logging.error(f"Error adding wanted items: {str(e)}", exc_info=True)
        conn.rollback()
        raise
    finally:
        conn.close()