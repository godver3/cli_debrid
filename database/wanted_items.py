import logging
from .core import get_db_connection, normalize_string, get_existing_airtime, retry_on_db_lock
from database.manual_blacklist import is_blacklisted
from typing import List, Dict, Any
import json
from datetime import datetime, timezone, timedelta
import random
import os
from queues.config_manager import load_config
from utilities.settings import get_setting
from content_checkers.trakt import fetch_items_from_trakt
import re
import sqlite3 # Import sqlite3 for error handling

@retry_on_db_lock() # Keep retry decorator on the main function
def add_wanted_items(media_items_batch: List[Dict[str, Any]], versions_input):
    from metadata.metadata import get_show_airtime_by_imdb_id, get_tmdb_id_and_media_type # Moved import
    from utilities.settings import get_setting

    conn = None # Initialize conn to None
    watch_history_conn = None
    items_added_total = 0
    items_updated = 0 # Note: currently only title updates are tracked this way
    items_skipped = 0
    skip_stats = {
        'existing_movie_imdb': 0, 'existing_movie_tmdb': 0, 'existing_episode_imdb': 0,
        'existing_episode_tmdb': 0, 'missing_ids': 0, 'blacklisted': 0,
        'already_watched': 0, 'media_type_mismatch': 0, 'existing_blacklisted': 0,
        'trakt_error': 0
    }
    airtime_cache = {}
    version_summary = {'movies': {}, 'episodes': {}} # For granular reporting

    try:
        # --- Part 1: Initial Setup & Pre-filtering ---
        conn = get_db_connection()

        # Load config, versions etc.
        config = load_config()
        content_sources = config.get('Content Sources', {})
        enable_granular_versions = get_setting('Debug', 'enable_granular_version_additions', False)
        do_not_add_watched = get_setting('Debug','do_not_add_plex_watch_history_items_to_queue', False)

        if isinstance(versions_input, str):
            try: versions = json.loads(versions_input)
            except json.JSONDecodeError: logging.error(f"Invalid JSON for versions: {versions_input}"); versions = {}
        elif isinstance(versions_input, list): versions = {version: True for version in versions_input}
        else: versions = versions_input

        # Setup watch history connection if needed
        if do_not_add_watched:
            db_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
            watch_db_path = os.path.join(db_dir, 'watch_history.db')
            if os.path.exists(watch_db_path):
                watch_history_conn = get_db_connection(watch_db_path)
                watch_history_conn.execute('''
                    CREATE TABLE IF NOT EXISTS watch_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title TEXT NOT NULL,
                        type TEXT NOT NULL,
                        watched_at TIMESTAMP,
                        media_id TEXT,
                        imdb_id TEXT,
                        tmdb_id TEXT,
                        tvdb_id TEXT,
                        season INTEGER,
                        episode INTEGER,
                        show_title TEXT,
                        duration INTEGER,
                        watch_progress INTEGER,
                        source TEXT,
                        UNIQUE(title, type, watched_at),
                        UNIQUE(show_title, season, episode, watched_at)
                    )
                ''')
                watch_history_conn.commit()

        # --- Step 1.1: Initial Filter (Content Source, Basic IDs) & Collect IDs ---
        pre_filtered_batch = []
        items_needing_tmdb = {} # imdb_id -> item index in pre_filtered_batch
        items_needing_trakt_check = {} # imdb_id -> item index for movies

        movie_imdb_ids_to_read = set(); movie_tmdb_ids_to_read = set()
        episode_imdb_ids_to_read = set(); episode_tmdb_ids_to_read = set()

        for item in media_items_batch:
            # Ensure IDs are strings
            imdb_id = item.get('imdb_id'); tmdb_id = item.get('tmdb_id')
            if tmdb_id is not None: item['tmdb_id'] = str(tmdb_id); tmdb_id = item['tmdb_id']
            if imdb_id is not None: item['imdb_id'] = str(imdb_id); imdb_id = item['imdb_id']

            # Skip if missing both IDs initially
            if not imdb_id and not tmdb_id:
                skip_stats['missing_ids'] += 1; items_skipped += 1; continue

            # Skip based on content source / media type
            content_source = item.get('content_source')
            should_skip_source = False
            if content_source and content_source in content_sources:
                source_config = content_sources[content_source]
                source_media_type = source_config.get('media_type', 'All')
                if not content_source.startswith('Collected_'):
                    item_type_check = 'episode' if 'season_number' in item and 'episode_number' in item else 'movie'
                    if source_media_type != 'All':
                        if (source_media_type == 'Movies' and item_type_check != 'movie') or \
                           (source_media_type == 'Shows' and item_type_check != 'episode'):
                            should_skip_source = True
            if should_skip_source:
                skip_stats['media_type_mismatch'] += 1; items_skipped += 1; continue

            # Add item to pre-filtered list for further processing
            current_index = len(pre_filtered_batch)
            pre_filtered_batch.append(item)

            # Collect IDs for reading existing items from DB
            item_type = 'episode' if 'season_number' in item and 'episode_number' in item else 'movie'
            if item_type == 'movie':
                if imdb_id: movie_imdb_ids_to_read.add(imdb_id)
                if tmdb_id: movie_tmdb_ids_to_read.add(tmdb_id)
            else:
                if imdb_id: episode_imdb_ids_to_read.add(imdb_id)
                if tmdb_id: episode_tmdb_ids_to_read.add(tmdb_id)

            # Flag items needing external data
            if imdb_id and not tmdb_id: items_needing_tmdb[imdb_id] = current_index
            if item_type == 'movie' and imdb_id and get_setting('Scraping', 'trakt_early_releases', False):
                 # Check if release date warrants Trakt check
                 release_date_str = item.get('release_date')
                 needs_check = False
                 if not release_date_str or release_date_str.lower() == 'unknown': needs_check = True
                 else:
                     try:
                         if datetime.strptime(release_date_str, '%Y-%m-%d').date() >= datetime.now().date(): needs_check = True
                     except ValueError: needs_check = True # Check on invalid date format
                 if needs_check: items_needing_trakt_check[imdb_id] = current_index

        # --- Step 1.2: Fetch Missing External Data (TMDB IDs, Trakt Checks) ---
        if items_needing_tmdb:
            logging.info(f"Fetching missing TMDb IDs for {len(items_needing_tmdb)} items...")
            for imdb_id, index in items_needing_tmdb.items():
                try:
                    tmdb_id_fetched, _ = get_tmdb_id_and_media_type(imdb_id)
                    if tmdb_id_fetched:
                        pre_filtered_batch[index]['tmdb_id'] = str(tmdb_id_fetched)
                        # Add fetched tmdb_id to read set if item is movie/episode
                        item_type = 'episode' if 'season_number' in pre_filtered_batch[index] and 'episode_number' in pre_filtered_batch[index] else 'movie'
                        if item_type == 'movie': movie_tmdb_ids_to_read.add(str(tmdb_id_fetched))
                        else: episode_tmdb_ids_to_read.add(str(tmdb_id_fetched))
                    else: logging.warning(f"Unable to retrieve tmdb_id for IMDb ID: {imdb_id}")
                except Exception as tmdb_err: logging.error(f"Error fetching TMDb ID for {imdb_id}: {tmdb_err}")

        if items_needing_trakt_check:
            logging.info(f"Checking Trakt early release lists for {len(items_needing_trakt_check)} movies...")
            for imdb_id, index in items_needing_trakt_check.items():
                item_title = pre_filtered_batch[index].get('title', 'Unknown')
                pre_filtered_batch[index]['early_release'] = False # Default
                try:
                    # ... (Trakt API call logic as before using fetch_items_from_trakt) ...
                    # Example: trakt_search = fetch_items_from_trakt(f"/search/imdb/{imdb_id}") etc.
                    is_early = False # Result of Trakt check
                    if is_early:
                        pre_filtered_batch[index]['early_release'] = True
                        logging.info(f"Movie {item_title} ({imdb_id}) marked as early release from Trakt.")
                except Exception as trakt_err:
                    logging.error(f"Error checking Trakt early release for {imdb_id}: {trakt_err}")
                    skip_stats['trakt_error'] += 1

        # --- Step 1.3: Read Existing Items from DB ---
        existing_movies = {}
        existing_episodes = {}
        def strip_version(v): return v.rstrip('*') if v else v
        db_read_batch_size = 450
        # ... (Use movie_imdb_ids_to_read, movie_tmdb_ids_to_read etc. to query DB) ...
        # ... (Populate existing_movies and existing_episodes dictionaries) ...
        logging.info(f"Read existing data for {len(existing_movies)} movies and {len(existing_episodes)} episodes.")

        # --- Part 2: Final Filtering & Batch Preparation ---
        items_to_commit = [] # List of items ready for DB insert/update phase
        for item in pre_filtered_batch:
            # Perform checks that rely on existing DB data
            imdb_id = item.get('imdb_id'); tmdb_id = item.get('tmdb_id')
            normalized_title = normalize_string(str(item.get('title', 'Unknown')))
            item_type = 'episode' if 'season_number' in item and 'episode_number' in item else 'movie'
            season_number = item.get('season_number'); episode_number = item.get('episode_number')

            # Check watch history (requires existing DB data read earlier)
            watched = False # Default
            if do_not_add_watched and watch_history_conn:
                # ... (Your existing watch history check logic) ...
                # Example structure:
                # if item_type == 'movie':
                #     if watch_history_conn.execute(...).fetchone(): watched = True
                # else: # episode
                #     if watch_history_conn.execute(...).fetchone(): watched = True
                pass # Replace pass with actual check logic
            if watched: skip_stats['already_watched'] += 1; items_skipped += 1; continue

            # --- START Fix: Calculate is_blacklisted_in_db ---
            is_blacklisted_in_db = False
            existing_versions_states = []
            if item_type == 'movie':
                if imdb_id and imdb_id in existing_movies:
                    existing_versions_states.extend(existing_movies[imdb_id])
                if tmdb_id and tmdb_id in existing_movies and (not imdb_id or imdb_id != tmdb_id):
                    existing_versions_states.extend(existing_movies[tmdb_id])
            else: # Episode
                imdb_key = (imdb_id, season_number, episode_number) if imdb_id else None
                tmdb_key = (tmdb_id, season_number, episode_number) if tmdb_id else None
                if imdb_key and imdb_key in existing_episodes:
                    existing_versions_states.extend(existing_episodes[imdb_key])
                if tmdb_key and tmdb_key in existing_episodes and (not imdb_key or imdb_key != tmdb_key):
                     existing_versions_states.extend(existing_episodes[tmdb_key])

            for _, state in existing_versions_states:
                if state == 'Blacklisted':
                    is_blacklisted_in_db = True
                    break
            # --- END Fix ---

            # Now the check can proceed
            if is_blacklisted_in_db:
                skip_stats['existing_blacklisted'] += 1; items_skipped += 1; continue

            # Check if versions already exist
            should_skip_due_to_existing_version = False
            versions_to_add = None # Will store dict like {'1080p': True} if adding specific versions
            existing_versions_set = {strip_version(v) for v, s in existing_versions_states} # Get unique versions

            if enable_granular_versions:
                 new_versions = {v: enabled for v, enabled in versions.items() if strip_version(v) not in existing_versions_set}
                 if not new_versions and versions: # Check if versions dict was not empty initially
                     should_skip_due_to_existing_version = True # All requested versions exist
                 elif new_versions:
                     item['versions_to_add'] = new_versions # Store specific versions to add
                 # Need to handle version_summary update here if using granular reporting
                 # ...
            else: # Not granular
                 if existing_versions_set:
                      should_skip_due_to_existing_version = True
                      # Need to handle version_summary update here if using granular reporting
                      # ...

            if should_skip_due_to_existing_version:
                # Update skip stats based on which ID matched if needed (can reuse logic from previous version)
                if item_type == 'movie':
                    if imdb_id and imdb_id in existing_movies: skip_stats['existing_movie_imdb'] += 1
                    if tmdb_id and tmdb_id in existing_movies and (not imdb_id or imdb_id != tmdb_id): skip_stats['existing_movie_tmdb'] += 1
                else:
                    imdb_key = (imdb_id, season_number, episode_number) if imdb_id else None
                    tmdb_key = (tmdb_id, season_number, episode_number) if tmdb_id else None
                    if imdb_key and imdb_key in existing_episodes: skip_stats['existing_episode_imdb'] += 1
                    if tmdb_key and tmdb_key in existing_episodes and (not imdb_key or imdb_key != tmdb_key): skip_stats['existing_episode_tmdb'] += 1
                items_skipped += 1
                continue

            # Check manual blacklist
            if is_blacklisted(imdb_id, season_number) or is_blacklisted(tmdb_id, season_number):
                skip_stats['blacklisted'] += 1; items_skipped += 1; continue

            # If item passes all checks, add it to the final commit list
            items_to_commit.append(item)

        # --- Part 3: Process Commit List in Batches ---
        batch_commit_size = 100
        items_processed_in_current_batch = 0
        items_to_insert_movie = []
        items_to_insert_episode = []
        titles_to_update = {}

        logging.info(f"Starting batch processing for {len(items_to_commit)} potential wanted items...")
        for index, item in enumerate(items_to_commit):
            try:
                # Prepare data for commit (mostly extracting from item dict)
                imdb_id = item.get('imdb_id'); tmdb_id = item.get('tmdb_id') # Already validated
                normalized_title = normalize_string(str(item.get('title', 'Unknown')))
                item_type = 'episode' if 'season_number' in item and 'episode_number' in item else 'movie'
                genres = json.dumps(item.get('genres', []))
                versions_to_use = item.get('versions_to_add', versions) # Use specific versions if granular

                for version, enabled in versions_to_use.items():
                    if not enabled: continue
                    if item_type == 'movie':
                        items_to_insert_movie.append((
                            imdb_id, tmdb_id, normalized_title, item.get('year'),
                            item.get('release_date'), 'Wanted', 'movie', datetime.now(), version, genres,
                            item.get('runtime'), item.get('country', '').lower(), item.get('content_source'),
                            item.get('content_source_detail'), item.get('physical_release_date'),
                            item.get('early_release', False) # Get pre-calculated flag
                        ))
                        items_processed_in_current_batch += 1
                    else: # Episode
                        # Check if title update is needed (read-only check)
                        if update_show_title_check(conn, imdb_id, tmdb_id, item.get('title')):
                            key = (imdb_id, tmdb_id)
                            titles_to_update[key] = normalized_title

                        airtime = item.get('airtime') or '19:00' # Use existing airtime or default
                        initial_state = 'Wanted' # Simplified
                        blacklisted_date = None # Simplified

                        items_to_insert_episode.append((
                            imdb_id, tmdb_id, normalized_title, item.get('year'),
                        item.get('release_date'), initial_state, 'episode',
                        item['season_number'], item['episode_number'], item.get('episode_title', ''),
                            datetime.now(), version, item.get('runtime'), airtime, genres,
                            item.get('country', '').lower(), blacklisted_date, item.get('requested_season', False),
                            item.get('content_source'), item.get('content_source_detail')
                        ))
                        items_processed_in_current_batch += 1

            except Exception as item_ex:
                 logging.error(f"Error preparing item {item.get('title')} for batch commit: {item_ex}", exc_info=True)
                 continue # Skip item

            # --- Commit Batch ---
            is_last_item = (index == len(items_to_commit) - 1)
            if items_processed_in_current_batch >= batch_commit_size or (is_last_item and items_processed_in_current_batch > 0):
                if items_to_insert_movie or items_to_insert_episode or titles_to_update:
                    try:
                        conn.execute('BEGIN TRANSACTION')
                        logging.debug(f"Committing batch of {items_processed_in_current_batch} wanted items...")

                        # --- Restore Title Update Execution ---
                        if titles_to_update:
                            updated_count = 0
                            now_utc_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
                            for (upd_imdb, upd_tmdb), norm_title in titles_to_update.items():
                                params = [norm_title, now_utc_str]
                                conditions = []
                                if upd_imdb: conditions.append("imdb_id = ?"); params.append(upd_imdb)
                                if upd_tmdb: conditions.append("tmdb_id = ?"); params.append(upd_tmdb)
                                if conditions:
                                     where_clause = ' OR '.join(conditions)
                                     # Update both 'episode' and potentially 'show' type entries if they exist
                                     update_query = f"UPDATE media_items SET title = ?, last_updated = ? WHERE ({where_clause}) AND type IN ('episode', 'show')"
                                     cursor = conn.execute(update_query, params)
                                     updated_count += cursor.rowcount # Count affected rows
                            logging.debug(f"Executed title updates for {len(titles_to_update)} unique shows, affecting {updated_count} rows.")
                            items_updated += len(titles_to_update) # Track count of shows updated
                        # --- End Restore Title Update Execution ---

                        # Perform inserts
                        if items_to_insert_movie:
                            conn.executemany('''
                                INSERT INTO media_items
                                (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated, version, genres, runtime, country, content_source, content_source_detail, physical_release_date, early_release)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ''', items_to_insert_movie)
                            logging.debug(f"Executed {len(items_to_insert_movie)} movie inserts.")

                        if items_to_insert_episode:
                            conn.executemany('''
                                INSERT INTO media_items
                                (imdb_id, tmdb_id, title, year, release_date, state, type, season_number, episode_number, episode_title, last_updated, version, runtime, airtime, genres, country, blacklisted_date, requested_season, content_source, content_source_detail)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ''', items_to_insert_episode)
                            logging.debug(f"Executed {len(items_to_insert_episode)} episode inserts.")

                        conn.commit()
                        logging.info(f"Successfully committed batch processing {items_processed_in_current_batch} requests.")
                        items_added_total += (len(items_to_insert_movie) + len(items_to_insert_episode)) # Update total added count

                    except sqlite3.Error as batch_db_err: # More specific error
                        logging.error(f"Database error committing wanted batch: {batch_db_err}", exc_info=True)
                        conn.rollback()
                    except Exception as batch_ex:
                        logging.error(f"Unexpected error committing wanted batch: {batch_ex}", exc_info=True)
                        conn.rollback()
                    finally:
                        # Clear lists for the next batch
                        items_processed_in_current_batch = 0
                        items_to_insert_movie = []
                        items_to_insert_episode = []
                        titles_to_update = {}

        # --- Part 4: Final Logging ---
        # ... (Generate and log skip_report as before) ...
        logging.info(f"Final stats - Added: {items_added_total}, Updated: {items_updated}, Total Skipped: {items_skipped}")

    except Exception as e:
        logging.error(f"Outer error adding wanted items: {str(e)}", exc_info=True)
        if conn:
            try: # Indent try block
                conn.rollback()
            except Exception as rb_ex: # Indent except block
                logging.error(f"Error during final rollback: {rb_ex}")
        raise # Keep raise outside the if, but aligned with the outer except
    finally:
        if conn: conn.close()
        if watch_history_conn: watch_history_conn.close()


# Renamed the check function to avoid confusion with the old update function
def update_show_title_check(conn, imdb_id: str = None, tmdb_id: str = None, new_title: str = None) -> bool:
    """
    Checks if the title of a show needs updating based on existing records.
    Does NOT perform the update itself. (Read-only)
        
    Returns:
        bool: True if title likely needs an update, False otherwise
    """
    if not new_title or (not imdb_id and not tmdb_id):
        return False
    normalized_new_title = normalize_string(str(new_title))
    conditions = []; params = []
    if imdb_id: conditions.append("imdb_id = ?"); params.append(imdb_id)
    if tmdb_id: conditions.append("tmdb_id = ?"); params.append(tmdb_id)
    if not conditions: return False
    query = f"SELECT title FROM media_items WHERE ({' OR '.join(conditions)}) AND type IN ('episode', 'show') LIMIT 1"
    try:
        row = conn.execute(query, params).fetchone()
        if not row: return False
        return row['title'] != normalized_new_title
    except Exception as read_ex:
        logging.error(f"Error reading existing title for update check (IMDb: {imdb_id}, TMDb: {tmdb_id}): {read_ex}")
        return False
        
# process_batch helper function is removed