import logging
from .core import get_db_connection, normalize_string, get_existing_airtime
from manual_blacklist import is_blacklisted
from typing import List, Dict, Any
import json
from datetime import datetime, timezone
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

        movie_imdb_ids = set()
        movie_tmdb_ids = set()
        episode_imdb_ids = set()
        episode_tmdb_ids = set()
        episode_imdb_keys = set()
        episode_tmdb_keys = set()

        for item in media_items_batch:
            imdb_id = item.get('imdb_id')
            tmdb_id = item.get('tmdb_id')

            if tmdb_id is not None:
                tmdb_id = str(tmdb_id)
                item['tmdb_id'] = tmdb_id

            if imdb_id is not None:
                imdb_id = str(imdb_id)
                item['imdb_id'] = imdb_id

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

        existing_movies = set()
        batch_size = 450

        if movie_imdb_ids:
            movie_imdb_list = list(movie_imdb_ids)
            for i in range(0, len(movie_imdb_list), batch_size):
                batch = movie_imdb_list[i:i + batch_size]
                placeholders = ', '.join(['?'] * len(batch))
                query = f'''
                    SELECT imdb_id FROM media_items
                    WHERE type = 'movie' AND imdb_id IN ({placeholders})
                '''
                rows = conn.execute(query, tuple(batch)).fetchall()
                existing_movies.update(str(row['imdb_id']) for row in rows)

        if movie_tmdb_ids:
            movie_tmdb_list = list(movie_tmdb_ids)
            for i in range(0, len(movie_tmdb_list), batch_size):
                batch = movie_tmdb_list[i:i + batch_size]
                placeholders = ', '.join(['?'] * len(batch))
                query = f'''
                    SELECT tmdb_id FROM media_items
                    WHERE type = 'movie' AND tmdb_id IN ({placeholders})
                '''
                rows = conn.execute(query, tuple(batch)).fetchall()
                existing_movies.update(str(row['tmdb_id']) for row in rows)

        existing_episodes = set()

        if episode_imdb_ids:
            episode_imdb_list = list(episode_imdb_ids)
            for i in range(0, len(episode_imdb_list), batch_size):
                batch = episode_imdb_list[i:i + batch_size]
                placeholders = ', '.join(['?'] * len(batch))
                query = f'''
                    SELECT imdb_id, season_number, episode_number FROM media_items
                    WHERE type = 'episode' AND imdb_id IN ({placeholders})
                '''
                rows = conn.execute(query, tuple(batch)).fetchall()
                for row in rows:
                    key = (str(row['imdb_id']), row['season_number'], row['episode_number'])
                    existing_episodes.add(key)

        if episode_tmdb_ids:
            episode_tmdb_list = list(episode_tmdb_ids)
            for i in range(0, len(episode_tmdb_list), batch_size):
                batch = episode_tmdb_list[i:i + batch_size]
                placeholders = ', '.join(['?'] * len(batch))
                query = f'''
                    SELECT tmdb_id, season_number, episode_number FROM media_items
                    WHERE type = 'episode' AND tmdb_id IN ({placeholders})
                '''
                rows = conn.execute(query, tuple(batch)).fetchall()
                for row in rows:
                    key = (str(row['tmdb_id']), row['season_number'], row['episode_number'])
                    existing_episodes.add(key)

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
                    continue

            filtered_media_items_batch.append(item)

        media_items_batch = filtered_media_items_batch

        for item in media_items_batch:
            if not item.get('imdb_id') and not item.get('tmdb_id'):
                logging.warning(f"Skipping item without valid IMDb ID or TMDb ID: {item.get('title', 'Unknown')}")
                items_skipped += 1
                continue

            if is_blacklisted(item.get('imdb_id', '')) or is_blacklisted(item.get('tmdb_id', '')):
                items_skipped += 1
                continue

            if not item.get('tmdb_id'):
                tmdb_id, media_type = get_tmdb_id_and_media_type(item['imdb_id'])
                if tmdb_id:
                    item['tmdb_id'] = str(tmdb_id)
                else:
                    logging.warning(f"Unable to retrieve tmdb_id for {item.get('title', 'Unknown')} (IMDb ID: {item['imdb_id']})")

            normalized_title = normalize_string(str(item.get('title', 'Unknown')))
            item_type = 'episode' if 'season_number' in item and 'episode_number' in item else 'movie'

            genres = json.dumps(item.get('genres', []))

            for version, enabled in versions.items():
                if not enabled:
                    continue

                if item_type == 'movie':
                    conn.execute('''
                        INSERT INTO media_items
                        (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated, version, genres, runtime, country)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        item.get('imdb_id'), item.get('tmdb_id'), normalized_title, item.get('year'),
                        item.get('release_date'), 'Wanted', 'movie', datetime.now(), version, genres, item.get('runtime'), item.get('country', '').lower()
                    ))
                    items_added += 1
                else:
                    airtime = item.get('airtime') or '19:00'
                    
                    from settings import get_setting

                    if get_setting('Debug', 'allow_partial_overseerr_requests'):
                        initial_state = 'Wanted' if item.get('is_requested_season', True) else 'Blacklisted'
                    else:
                        initial_state = 'Wanted'
                    blacklisted_date = datetime.now(timezone.utc) if initial_state == 'Blacklisted' else None

                    conn.execute('''
                        INSERT INTO media_items
                        (imdb_id, tmdb_id, title, year, release_date, state, type, season_number, episode_number, 
                         episode_title, last_updated, version, runtime, airtime, genres, country, blacklisted_date,
                         requested_season)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        item.get('imdb_id'), item.get('tmdb_id'), normalized_title, item.get('year'),
                        item.get('release_date'), initial_state, 'episode',
                        item['season_number'], item['episode_number'], item.get('episode_title', ''),
                        datetime.now(), version, item.get('runtime'), airtime, genres, item.get('country', '').lower(),
                        blacklisted_date, item.get('requested_season', False)
                    ))
                    items_added += 1

        conn.commit()
        logging.info(f"Wanted items processing complete. Added: {items_added}, Updated: {items_updated}, Skipped: {items_skipped}")
    except Exception as e:
        logging.error(f"Error adding wanted items: {str(e)}", exc_info=True)
        conn.rollback()
        raise
    finally:
        conn.close()