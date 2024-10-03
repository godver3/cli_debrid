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

        # Prepare sets of unique identifiers for movies and episodes
        movie_imdb_ids = set()
        movie_tmdb_ids = set()
        episode_imdb_ids = set()
        episode_tmdb_ids = set()
        episode_imdb_keys = set()
        episode_tmdb_keys = set()

        for item in media_items_batch:
            imdb_id = item.get('imdb_id')
            tmdb_id = item.get('tmdb_id')

            # Ensure consistent types for IDs
            if tmdb_id is not None:
                tmdb_id = str(tmdb_id)
                item['tmdb_id'] = tmdb_id  # Update item to use consistent tmdb_id

            if imdb_id is not None:
                imdb_id = str(imdb_id)
                item['imdb_id'] = imdb_id  # Update item to use consistent imdb_id

            item_type = 'episode' if 'season_number' in item and 'episode_number' in item else 'movie'

            if item_type == 'movie':
                if imdb_id:
                    movie_imdb_ids.add(imdb_id)
                if tmdb_id:
                    movie_tmdb_ids.add(tmdb_id)
            else:
                season_number = item.get('season_number')
                episode_number = item.get('episode_number')
                if imdb_id:
                    episode_imdb_ids.add(imdb_id)
                    episode_imdb_keys.add((imdb_id, season_number, episode_number))
                if tmdb_id:
                    episode_tmdb_ids.add(tmdb_id)
                    episode_tmdb_keys.add((tmdb_id, season_number, episode_number))

        # Fetch existing movies from the database
        existing_movies = set()
        if movie_imdb_ids:
            placeholders = ', '.join(['?'] * len(movie_imdb_ids))
            query = f'''
                SELECT imdb_id FROM media_items
                WHERE type = 'movie' AND imdb_id IN ({placeholders})
            '''
            rows = conn.execute(query, tuple(movie_imdb_ids)).fetchall()
            existing_movies.update(str(row['imdb_id']) for row in rows)

        if movie_tmdb_ids:
            placeholders = ', '.join(['?'] * len(movie_tmdb_ids))
            query = f'''
                SELECT tmdb_id FROM media_items
                WHERE type = 'movie' AND tmdb_id IN ({placeholders})
            '''
            rows = conn.execute(query, tuple(movie_tmdb_ids)).fetchall()
            existing_movies.update(str(row['tmdb_id']) for row in rows)

        # Fetch existing episodes from the database
        existing_episodes = set()

        # For episodes with imdb_id
        if episode_imdb_ids:
            placeholders = ', '.join(['?'] * len(episode_imdb_ids))
            query = f'''
                SELECT imdb_id, season_number, episode_number FROM media_items
                WHERE type = 'episode' AND imdb_id IN ({placeholders})
            '''
            rows = conn.execute(query, tuple(episode_imdb_ids)).fetchall()
            for row in rows:
                key = (str(row['imdb_id']), row['season_number'], row['episode_number'])
                existing_episodes.add(key)

        # For episodes with tmdb_id
        if episode_tmdb_ids:
            placeholders = ', '.join(['?'] * len(episode_tmdb_ids))
            query = f'''
                SELECT tmdb_id, season_number, episode_number FROM media_items
                WHERE type = 'episode' AND tmdb_id IN ({placeholders})
            '''
            rows = conn.execute(query, tuple(episode_tmdb_ids)).fetchall()
            for row in rows:
                key = (str(row['tmdb_id']), row['season_number'], row['episode_number'])
                existing_episodes.add(key)

        # Filter out existing items from media_items_batch
        filtered_media_items_batch = []
        for item in media_items_batch:
            imdb_id = item.get('imdb_id')
            tmdb_id = item.get('tmdb_id')
            item_type = 'episode' if 'season_number' in item and 'episode_number' in item else 'movie'

            if item_type == 'movie':
                skip = False
                if imdb_id and imdb_id in existing_movies:
                    skip = True
                if tmdb_id and tmdb_id in existing_movies:
                    skip = True
                if skip:
                    items_skipped += 1
                    logging.debug(f"Skipping existing movie: {item.get('title', 'Unknown')} (IMDb ID: {imdb_id}, TMDb ID: {tmdb_id})")
                    continue
            else:
                season_number = item.get('season_number')
                episode_number = item.get('episode_number')
                skip = False
                if imdb_id and (imdb_id, season_number, episode_number) in existing_episodes:
                    skip = True
                if tmdb_id and (tmdb_id, season_number, episode_number) in existing_episodes:
                    skip = True
                if skip:
                    items_skipped += 1
                    logging.debug(f"Skipping existing episode: {item.get('title', 'Unknown')} S{season_number}E{episode_number} (IMDb ID: {imdb_id}, TMDb ID: {tmdb_id})")
                    continue

            filtered_media_items_batch.append(item)

        # Proceed with adding new items
        media_items_batch = filtered_media_items_batch

        for item in media_items_batch:
            if not item.get('imdb_id') and not item.get('tmdb_id'):
                logging.warning(f"Skipping item without valid IMDb ID or TMDb ID: {item.get('title', 'Unknown')}")
                items_skipped += 1
                continue

            if is_blacklisted(item.get('imdb_id', '')) or is_blacklisted(item.get('tmdb_id', '')):
                logging.debug(f"Skipping blacklisted item: {item.get('title', 'Unknown')} (IMDb ID: {item.get('imdb_id')}, TMDb ID: {item.get('tmdb_id')})")
                items_skipped += 1
                continue

            if not item.get('tmdb_id'):
                tmdb_id, media_type = get_tmdb_id_and_media_type(item['imdb_id'])
                if tmdb_id:
                    item['tmdb_id'] = str(tmdb_id)  # Ensure tmdb_id is a string
                else:
                    logging.warning(f"Unable to retrieve tmdb_id for {item.get('title', 'Unknown')} (IMDb ID: {item['imdb_id']})")

            normalized_title = normalize_string(str(item.get('title', 'Unknown')))
            item_type = 'episode' if 'season_number' in item and 'episode_number' in item else 'movie'

            genres = json.dumps(item.get('genres', []))  # Convert genres list to JSON string

            for version, enabled in versions.items():
                if not enabled:
                    continue

                # Insert new items into the database
                if item_type == 'movie':
                    conn.execute('''
                        INSERT INTO media_items
                        (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated, version, genres, runtime)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        item.get('imdb_id'), item.get('tmdb_id'), normalized_title, item.get('year'),
                        item.get('release_date'), 'Wanted', 'movie', datetime.now(), version, genres, item.get('runtime')
                    ))
                    logging.debug(f"Adding new movie as Wanted in DB: {normalized_title} (Version: {version})")
                    items_added += 1
                else:
                    '''
                    # For episodes, get the airtime
                    show_id = item.get('imdb_id') or item.get('tmdb_id')
                    if show_id not in airtime_cache:
                        airtime_cache[show_id] = get_existing_airtime(conn, show_id)
                        if airtime_cache[show_id] is None:
                            logging.debug(f"No existing airtime found for show {show_id}, fetching from metadata")
                            airtime_cache[show_id] = get_show_airtime_by_imdb_id(show_id)

                        # Ensure we always have a default airtime
                        if not airtime_cache[show_id]:
                            airtime_cache[show_id] = '19:00'
                            logging.debug(f"No airtime found, defaulting to 19:00 for show {show_id}")

                        logging.debug(f"Airtime for show {show_id} set to {airtime_cache[show_id]}")
                    '''

                    airtime = item.get('airtime') or '19:00'

                    conn.execute('''
                        INSERT INTO media_items
                        (imdb_id, tmdb_id, title, year, release_date, state, type, season_number, episode_number, episode_title, last_updated, version, runtime, airtime, genres)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        item.get('imdb_id'), item.get('tmdb_id'), normalized_title, item.get('year'),
                        item.get('release_date'), 'Wanted', 'episode',
                        item['season_number'], item['episode_number'], item.get('episode_title', ''),
                        datetime.now(), version, item.get('runtime'), airtime, genres
                    ))
                    logging.debug(f"Adding new episode as Wanted in DB: {normalized_title} S{item['season_number']}E{item['episode_number']} (Version: {version})")
                    items_added += 1

        conn.commit()
        logging.debug(f"Wanted items processing complete. Added: {items_added}, Updated: {items_updated}, Skipped: {items_skipped}")
    except Exception as e:
        logging.error(f"Error adding wanted items: {str(e)}", exc_info=True)
        conn.rollback()
        raise
    finally:
        conn.close()