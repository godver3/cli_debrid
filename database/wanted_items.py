import logging
from .core import get_db_connection, normalize_string, get_existing_airtime
from manual_blacklist import is_blacklisted
from typing import List, Dict, Any
import json
from datetime import datetime
from metadata.metadata import get_tmdb_id_and_media_type
import random

def add_wanted_items(media_items_batch: List[Dict[str, Any]], versions: Dict[str, bool]):
    from metadata.metadata import get_show_airtime_by_imdb_id

    conn = get_db_connection()
    try:
        items_added = 0
        items_updated = 0
        items_skipped = 0
        airtime_cache = {}

        # Handle different types of versions input
        if isinstance(versions, str):
            try:
                versions = json.loads(versions)
            except json.JSONDecodeError:
                logging.error(f"Invalid JSON string for versions: {versions}")
                versions = {}
        elif isinstance(versions, list):
            versions = {version: True for version in versions}

        # New filter step: Collect all items from the database with any state
        all_existing_items = conn.execute('''
            SELECT imdb_id, tmdb_id, type, season_number, episode_number, state, version
            FROM media_items
        ''').fetchall()

        # Create dictionaries for efficient lookup
        existing_movies = {}
        existing_episodes = {}
        for item in all_existing_items:
            if item['type'] == 'movie':
                existing_movies[item['imdb_id']] = item
                if item['tmdb_id']:
                    existing_movies[item['tmdb_id']] = item
            else:
                key = (item['imdb_id'], item['season_number'], item['episode_number'])
                existing_episodes[key] = item
                if item['tmdb_id']:
                    key = (item['tmdb_id'], item['season_number'], item['episode_number'])
                    existing_episodes[key] = item

        # Filter out items that already exist in the database
        filtered_media_items_batch = []
        filtered_out_count = 0
        for item in media_items_batch:
            item_type = 'episode' if 'season_number' in item and 'episode_number' in item else 'movie'
            
            if item_type == 'movie':
                if item.get('imdb_id') in existing_movies or item.get('tmdb_id') in existing_movies:
                    filtered_out_count += 1
                    if filtered_out_count <= 5:
                        logging.debug(f"Filtered out movie: {item.get('imdb_id')} / {item.get('tmdb_id')}")
                    continue
            else:
                imdb_key = (item.get('imdb_id'), item.get('season_number'), item.get('episode_number'))
                tmdb_key = (item.get('tmdb_id'), item.get('season_number'), item.get('episode_number'))
                if imdb_key in existing_episodes or tmdb_key in existing_episodes:
                    filtered_out_count += 1
                    if filtered_out_count <= 5:
                        logging.debug(f"Filtered out episode: {item.get('imdb_id')} / {item.get('tmdb_id')} - S{item.get('season_number')}E{item.get('episode_number')}")
                    continue
            
            filtered_media_items_batch.append(item)

        logging.debug(f"Filtered out {filtered_out_count} items that already exist in the database")

        for item in filtered_media_items_batch:
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

            # Check if any version of the item is already collected
            if item_type == 'movie':
                any_version_collected = conn.execute('''
                    SELECT id FROM media_items
                    WHERE (imdb_id = ? OR tmdb_id = ?) AND type = 'movie' AND state = 'Collected'
                ''', (item.get('imdb_id'), item.get('tmdb_id'))).fetchone()
            else:
                any_version_collected = conn.execute('''
                    SELECT id FROM media_items
                    WHERE (imdb_id = ? OR tmdb_id = ?) AND type = 'episode' AND season_number = ? AND episode_number = ? AND state = 'Collected'
                ''', (item.get('imdb_id'), item.get('tmdb_id'), item['season_number'], item['episode_number'])).fetchone()

            if any_version_collected:
                logging.debug(f"Skipping item as it's already collected in some version: {normalized_title}")
                items_skipped += 1
                continue

            genres = json.dumps(item.get('genres', []))  # Convert genres list to JSON string

            for version, enabled in versions.items():
                if not enabled:
                    continue

                # TODO: Add missing versions instead of just skipping add
                # Check if item exists for this version
                if item_type == 'movie':
                    existing_item = conn.execute('''
                        SELECT id, release_date, state FROM media_items
                        WHERE (imdb_id = ? OR tmdb_id = ?) AND type = 'movie' AND version = ?
                    ''', (item.get('imdb_id'), item.get('tmdb_id'), version)).fetchone()
                else:
                    existing_item = conn.execute('''
                        SELECT id, release_date, state FROM media_items
                        WHERE (imdb_id = ? OR tmdb_id = ?) AND type = 'episode' AND season_number = ? AND episode_number = ? AND version = ?
                    ''', (item.get('imdb_id'), item.get('tmdb_id'), item['season_number'], item['episode_number'], version)).fetchone()

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
                            item.get('season_number'), item.get('episode_number'), item.get('episode_title', ''),
                            datetime.now(), version, item.get('runtime'), airtime, genres
                        ))
                    logging.debug(f"Adding new {'movie' if item_type == 'movie' else 'episode'} as Wanted in DB: {normalized_title} (Version: {version})")
                    items_added += 1

        conn.commit()
        logging.debug(f"Wanted items processing complete. Added: {items_added}, Updated: {items_updated}, Skipped: {items_skipped}")
    except Exception as e:
        logging.error(f"Error adding wanted items: {str(e)}")
        conn.rollback()
    finally:
        conn.close()