import logging
from .core import get_db_connection, normalize_string, get_existing_airtime
from manual_blacklist import is_blacklisted
from typing import List, Dict, Any
import json
from datetime import datetime

def add_wanted_items(media_items_batch: List[Dict[str, Any]], versions: Dict[str, bool]):
    from metadata.metadata import get_show_airtime_by_imdb_id

    logging.debug(f"add_wanted_items called with versions type: {type(versions)}")
    logging.debug(f"versions content: {versions}")

    conn = get_db_connection()
    try:
        items_added = 0
        items_updated = 0
        items_skipped = 0

        # Handle different types of versions input
        if isinstance(versions, str):
            try:
                versions = json.loads(versions)
            except json.JSONDecodeError:
                logging.error(f"Invalid JSON string for versions: {versions}")
                versions = {}
        elif isinstance(versions, list):
            versions = {version: True for version in versions}

        logging.debug(f"Processed versions: {versions}")

        for item in media_items_batch:
            if not item.get('imdb_id'):
                logging.warning(f"Skipping item without valid IMDb ID: {item.get('title', 'Unknown')}")
                items_skipped += 1
                continue

            if is_blacklisted(item['imdb_id']):
                logging.debug(f"Skipping blacklisted item: {item.get('title', 'Unknown')} (IMDb ID: {item['imdb_id']})")
                items_skipped += 1
                continue

            normalized_title = normalize_string(str(item.get('title', 'Unknown')))
            item_type = 'episode' if 'season_number' in item and 'episode_number' in item else 'movie'

            # Check if any version of the item is already collected
            if item_type == 'movie':
                any_version_collected = conn.execute('''
                    SELECT id FROM media_items
                    WHERE imdb_id = ? AND type = 'movie' AND state = 'Collected'
                ''', (item['imdb_id'],)).fetchone()
            else:
                any_version_collected = conn.execute('''
                    SELECT id FROM media_items
                    WHERE imdb_id = ? AND type = 'episode' AND season_number = ? AND episode_number = ? AND state = 'Collected'
                ''', (item['imdb_id'], item['season_number'], item['episode_number'])).fetchone()

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
                        WHERE imdb_id = ? AND type = 'movie' AND version = ?
                    ''', (item['imdb_id'], version)).fetchone()
                else:
                    existing_item = conn.execute('''
                        SELECT id, release_date, state FROM media_items
                        WHERE imdb_id = ? AND type = 'episode' AND season_number = ? AND episode_number = ? AND version = ?
                    ''', (item['imdb_id'], item['season_number'], item['episode_number'], version)).fetchone()

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
                            item['imdb_id'], item.get('tmdb_id'), normalized_title, item.get('year'),
                            item.get('release_date'), 'Wanted', 'movie', datetime.now(), version, genres, item.get('runtime')
                        ))
                    else:                     
                        conn.execute('''
                            INSERT INTO media_items
                            (imdb_id, tmdb_id, title, year, release_date, state, type, season_number, episode_number, episode_title, last_updated, version, runtime, airtime, genres)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            item['imdb_id'], item.get('tmdb_id'), normalized_title, item.get('year'),
                            item.get('release_date'), 'Wanted', 'episode',
                            item['season_number'], item['episode_number'], item.get('episode_title', ''),
                            datetime.now(), version, item.get('runtime'), item.get('airtime'), genres
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