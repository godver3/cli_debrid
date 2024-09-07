from .core import get_db_connection, row_to_dict, normalize_string, get_existing_airtime
import logging
import os
from datetime import datetime
import json
from fuzzywuzzy import fuzz
from settings import get_setting

def add_collected_items(media_items_batch, recent=False):
    from metadata.metadata import get_show_airtime_by_imdb_id

    conn = get_db_connection()
    try:
        conn.execute('BEGIN TRANSACTION')
        
        processed_items = set()
        airtime_cache = {}  # Cache to store airtimes for each show

        existing_items = conn.execute('SELECT id, imdb_id, tmdb_id, type, season_number, episode_number, state, version, filled_by_file FROM media_items').fetchall()
        existing_ids = {}
        for row in map(row_to_dict, existing_items):
            if row['type'] == 'movie':
                imdb_key = (row['imdb_id'], 'movie', row['version'])
                tmdb_key = (row['tmdb_id'], 'movie', row['version']) if row['tmdb_id'] else None
            else:
                imdb_key = (row['imdb_id'], 'episode', row['season_number'], row['episode_number'], row['version'])
                tmdb_key = (row['tmdb_id'], 'episode', row['season_number'], row['episode_number'], row['version']) if row['tmdb_id'] else None
            
            item_data = (row['id'], row['state'], row['filled_by_file'])
            existing_ids[imdb_key] = item_data
            if tmdb_key:
                existing_ids[tmdb_key] = item_data

        scraping_versions = get_setting('Scraping', 'versions', {})
        versions = list(scraping_versions.keys())

        for item in media_items_batch:
            imdb_id = item.get('imdb_id')
            tmdb_id = item.get('tmdb_id')
            
            logging.debug(f"Processing item: Title: {item.get('title', 'Unknown')}, IMDb ID: {imdb_id}, TMDB ID: {tmdb_id}")
            
            # Check if imdb_id is None or the string "None"
            if imdb_id == "None" or imdb_id is None:
                imdb_id = None
            
            if not imdb_id and not tmdb_id:
                logging.warning(f"Skipping item without valid IMDb ID or TMDB ID: {item.get('title', 'Unknown')}")
                continue

            normalized_title = normalize_string(item.get('title', 'Unknown'))
            item_type = 'episode' if 'season_number' in item and 'episode_number' in item else 'movie'

            plex_filename = os.path.basename(item.get('location', ''))

            genres = json.dumps(item.get('genres', []))  # Convert genres list to JSON string

            item_found = False
            item_id = None  # Initialize item_id here
            current_state = None
            filled_by_file = None

            for version in versions:
                if item_type == 'movie':
                    imdb_key = (imdb_id, 'movie', version) if imdb_id else None
                    tmdb_key = (tmdb_id, 'movie', version) if tmdb_id else None
                else:
                    imdb_key = (imdb_id, 'episode', item['season_number'], item['episode_number'], version) if imdb_id else None
                    tmdb_key = (tmdb_id, 'episode', item['season_number'], item['episode_number'], version) if tmdb_id else None

                if imdb_key in existing_ids:
                    item_found = True
                    item_id, current_state, filled_by_file = existing_ids[imdb_key]
                elif tmdb_key and tmdb_key in existing_ids:
                    item_found = True
                    item_id, current_state, filled_by_file = existing_ids[tmdb_key]

                if item_found:
                    logging.debug(f"Existing item found: ID: {item_id}, State: {current_state}, Filled by file: {filled_by_file}")
                    logging.debug(f"Comparing existing item in DB with Plex item:")
                    logging.debug(f"  DB Item: {normalized_title} (ID: {item_id}, State: {current_state}, Version: {version})")
                    logging.debug(f"  DB Filled By File: {filled_by_file}")
                    logging.debug(f"  Plex Filename: {plex_filename}")

                    if filled_by_file and plex_filename:
                        match_ratio = fuzz.ratio(filled_by_file.lower(), plex_filename.lower())
                        logging.debug(f"  Fuzzy match ratio: {match_ratio}%")

                        if match_ratio >= 95:  # You can adjust this threshold
                            logging.debug(f"  Match found: DB Filled By File matches Plex Filename (Fuzzy match: {match_ratio}%)")

                            if current_state != 'Collected':
                                conn.execute('''
                                    UPDATE media_items
                                    SET state = ?, last_updated = ?, collected_at = ?
                                    WHERE id = ?
                                ''', ('Collected', datetime.now(), datetime.now(), item_id))

                                # Check for other episodes with the same filled_by_file
                                cursor = conn.execute('''
                                    SELECT id, state FROM media_items
                                    WHERE filled_by_file = ? AND state = 'Checking'
                                ''', (filled_by_file,))
                                related_episodes = cursor.fetchall()

                                for related_id, related_state in related_episodes:
                                    conn.execute('''
                                        UPDATE media_items
                                        SET state = ?, last_updated = ?, collected_at = ?
                                        WHERE id = ?
                                    ''', ('Collected', datetime.now(), datetime.now(), related_id))

                    processed_items.add(item_id)
                    break  # Exit the version loop if item is found

            if not item_found:
                logging.debug("Item not found in existing database, preparing to add new item")
                if item_type == 'movie':
                    cursor = conn.execute('''
                        SELECT id FROM media_items
                        WHERE (imdb_id = ? OR tmdb_id = ?) AND type = 'movie' AND version = 'unknown'
                    ''', (imdb_id, tmdb_id))
                else:
                    # For episodes, get the airtime
                    if imdb_id not in airtime_cache:
                        airtime_cache[imdb_id] = get_existing_airtime(conn, imdb_id)
                        if airtime_cache[imdb_id] is None:
                            logging.debug(f"No existing airtime found for show {imdb_id}, fetching from metadata")
                            airtime_cache[imdb_id] = get_show_airtime_by_imdb_id(imdb_id)
                        
                        # Ensure we always have a default airtime
                        if not airtime_cache[imdb_id]:
                            airtime_cache[imdb_id] = '19:00'
                            logging.debug(f"No airtime found, defaulting to 19:00 for show {imdb_id}")
                        
                        logging.debug(f"Airtime for show {imdb_id} set to {airtime_cache[imdb_id]}")
                    
                    airtime = airtime_cache[imdb_id]

                    cursor = conn.execute('''
                        SELECT id FROM media_items
                        WHERE (imdb_id = ? OR tmdb_id = ?) AND type = 'episode' AND season_number = ? AND episode_number = ? AND version = 'unknown'
                    ''', (imdb_id, tmdb_id, item['season_number'], item['episode_number']))

                existing_item = cursor.fetchone()

                if existing_item:
                    item_id = existing_item[0]
                    if item_type == 'movie':
                        conn.execute('''
                            UPDATE media_items
                            SET state = ?, last_updated = ?
                            WHERE id = ?
                        ''', ('Collected', datetime.now(), item_id))
                    else:
                        conn.execute('''
                            UPDATE media_items
                            SET state = ?, last_updated = ?, airtime = ?
                            WHERE id = ?
                        ''', ('Collected', datetime.now(), airtime, item_id))
                        logging.debug(f"Updated existing item to Collected: {normalized_title} (ID: {item_id})")
                else:
                    if item_type == 'movie':
                        cursor = conn.execute('''
                            INSERT INTO media_items
                            (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated, metadata_updated, version, collected_at, genres)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                        ''', (
                            imdb_id, tmdb_id, normalized_title, item.get('year'),
                            item.get('release_date'), 'Collected', 'movie',
                            datetime.now(), datetime.now(), 'unknown', datetime.now(), genres
                        ))
                    else:
                        cursor = conn.execute('''
                            INSERT INTO media_items
                            (imdb_id, tmdb_id, title, year, release_date, state, type, season_number, episode_number, episode_title, last_updated, metadata_updated, version, airtime, collected_at, genres)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ''', (
                            imdb_id, tmdb_id, normalized_title, item.get('year'),
                            item.get('release_date'), 'Collected', 'episode',
                            item['season_number'], item['episode_number'], item.get('episode_title', ''),
                            datetime.now(), datetime.now(), 'unknown', airtime, datetime.now(), genres
                        ))
                        logging.info(f"Added new episode to DB as Collected: {normalized_title} S{item['season_number']}E{item['episode_number']} (Airtime: {airtime})")
                    item_id = cursor.lastrowid
                    logging.debug(f"Added new item to database: {normalized_title} (ID: {item_id})")
            
            processed_items.add(item_id)
  
        # Handle items not in the batch if not recent
        if not recent:
            items_to_check = set(id for id, _, _ in existing_ids.values()) - processed_items
            for item_id in items_to_check:
                item_info = conn.execute('SELECT title, state, version FROM media_items WHERE id = ?', (item_id,)).fetchone()
                if item_info:
                    title, state, version = item_info
                    if state not in ['Collected', 'Blacklisted', 'Unreleased', 'Wanted', 'Sleeping']:
                        conn.execute('UPDATE media_items SET state = ?, last_updated = ?, collected_at = NULL WHERE id = ?', ('Wanted', datetime.now(), item_id))
                        logging.debug(f"Moving item from '{state}' to 'Wanted' state: ID {item_id}, {title} (Version: {version})")

        conn.commit()
        logging.debug(f"Collected items processed and database updated. Total items: {len(media_items_batch)}, Processed: {len(processed_items)}")
    except Exception as e:
        logging.error(f"Error adding collected items: {str(e)}", exc_info=True)
        conn.rollback()
        raise
    finally:
        conn.close()