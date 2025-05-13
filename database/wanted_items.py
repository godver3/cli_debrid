import logging
from .core import get_db_connection, normalize_string, get_existing_airtime
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

def add_wanted_items(media_items_batch: List[Dict[str, Any]], versions_input):
    from metadata.metadata import get_show_airtime_by_imdb_id
    from utilities.settings import get_setting

    conn = get_db_connection()
    try:
        items_added = 0
        items_updated = 0
        items_skipped = 0
        skip_stats = {
            'existing_movie_imdb': 0,
            'existing_movie_tmdb': 0,
            'existing_episode_imdb': 0,
            'existing_episode_tmdb': 0,
            'missing_ids': 0,
            'blacklisted': 0,
            'already_watched': 0,
            'media_type_mismatch': 0,
            'existing_blacklisted': 0,  # Added for tracking skips due to existing blacklisted items
            'trakt_error': 0, # Added for tracking Trakt API errors
            'anime_filter': 0 # Added for tracking skips due to anime filter mode
        }
        airtime_cache = {}

        # Load config to get content source settings
        config = load_config()
        content_sources = config.get('Content Sources', {})

        # Check if we should skip watched content
        do_not_add_watched = get_setting('Debug','do_not_add_plex_watch_history_items_to_queue', False)
        watch_history_conn = None
        if do_not_add_watched:
            db_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
            watch_db_path = os.path.join(db_dir, 'watch_history.db')
            if os.path.exists(watch_db_path):
                watch_history_conn = get_db_connection(watch_db_path)
                # Ensure watch_history table exists
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

                # Check if source column exists, if not add it
                cursor = watch_history_conn.cursor()
                cursor.execute("PRAGMA table_info(watch_history)")
                columns = [column[1] for column in cursor.fetchall()]
                if 'source' not in columns:
                    watch_history_conn.execute('ALTER TABLE watch_history ADD COLUMN source TEXT')
                    watch_history_conn.commit()
                    logging.info("Added 'source' column to watch_history table")

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

        filtered_media_items_batch = []
        for item in media_items_batch:
            # Get content source settings
            content_source = item.get('content_source')
            if content_source and content_source in content_sources:
                source_config = content_sources[content_source]
                source_media_type = source_config.get('media_type', 'All')
                
                # Skip if content source is not Collected and media type doesn't match
                if not content_source.startswith('Collected_'):
                    item_type = 'episode' if 'season_number' in item and 'episode_number' in item else 'movie'
                    if source_media_type != 'All':
                        if (source_media_type == 'Movies' and item_type != 'movie') or \
                           (source_media_type == 'Shows' and item_type != 'episode'):
                            skip_stats['media_type_mismatch'] += 1
                            items_skipped += 1
                            continue

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

            filtered_media_items_batch.append(item)

        media_items_batch = filtered_media_items_batch

        # Get existing movies and episodes, including their state
        existing_movies = {}  # Stores {id: [(version, state), ...]}
        batch_size = 450
        
        def strip_version(version):
            """Strip asterisk from version for comparison"""
            return version.rstrip('*') if version else version

        # Track version additions for summary
        version_summary = {
            'movies': {},  # {id: {'existing': set(), 'added': set()}}
            'episodes': {}  # {(id, season, episode): {'existing': set(), 'added': set()}}
        }

        # Get the granular versions setting
        enable_granular_versions = get_setting('Debug', 'enable_granular_version_additions', False)

        if movie_imdb_ids:
            movie_imdb_list = list(movie_imdb_ids)
            for i in range(0, len(movie_imdb_list), batch_size):
                batch = movie_imdb_list[i:i + batch_size]
                placeholders = ', '.join(['?'] * len(batch))
                query = f'''
                    SELECT imdb_id, version, state FROM media_items
                    WHERE type = 'movie' AND imdb_id IN ({placeholders})
                '''
                rows = conn.execute(query, tuple(batch)).fetchall()
                for row in rows:
                    movie_id = str(row['imdb_id'])
                    if movie_id not in existing_movies:
                        existing_movies[movie_id] = []
                    existing_movies[movie_id].append((strip_version(row['version']), row['state']))

        if movie_tmdb_ids:
            movie_tmdb_list = list(movie_tmdb_ids)
            for i in range(0, len(movie_tmdb_list), batch_size):
                batch = movie_tmdb_list[i:i + batch_size]
                placeholders = ', '.join(['?'] * len(batch))
                query = f'''
                    SELECT tmdb_id, version, state FROM media_items
                    WHERE type = 'movie' AND tmdb_id IN ({placeholders})
                '''
                rows = conn.execute(query, tuple(batch)).fetchall()
                for row in rows:
                    movie_id = str(row['tmdb_id'])
                    if movie_id not in existing_movies:
                        existing_movies[movie_id] = []
                    existing_movies[movie_id].append((strip_version(row['version']), row['state']))

        existing_episodes = {}  # Stores {(id, season, episode): [(version, state), ...]}

        if episode_imdb_ids:
            episode_imdb_list = list(episode_imdb_ids)
            for i in range(0, len(episode_imdb_list), batch_size):
                batch = episode_imdb_list[i:i + batch_size]
                placeholders = ', '.join(['?'] * len(batch))
                query = f'''
                    SELECT imdb_id, season_number, episode_number, version, state FROM media_items
                    WHERE type = 'episode' AND imdb_id IN ({placeholders})
                '''
                rows = conn.execute(query, tuple(batch)).fetchall()
                for row in rows:
                    key = (str(row['imdb_id']), row['season_number'], row['episode_number'])
                    if key not in existing_episodes:
                        existing_episodes[key] = []
                    existing_episodes[key].append((strip_version(row['version']), row['state']))

        if episode_tmdb_ids:
            episode_tmdb_list = list(episode_tmdb_ids)
            for i in range(0, len(episode_tmdb_list), batch_size):
                batch = episode_tmdb_list[i:i + batch_size]
                placeholders = ', '.join(['?'] * len(batch))
                query = f'''
                    SELECT tmdb_id, season_number, episode_number, version, state FROM media_items
                    WHERE type = 'episode' AND tmdb_id IN ({placeholders})
                '''
                rows = conn.execute(query, tuple(batch)).fetchall()
                for row in rows:
                    key = (str(row['tmdb_id']), row['season_number'], row['episode_number'])
                    if key not in existing_episodes:
                        existing_episodes[key] = []
                    existing_episodes[key].append((strip_version(row['version']), row['state']))

        filtered_media_items_batch = []
        for item in media_items_batch:
            imdb_id = item.get('imdb_id')
            tmdb_id = item.get('tmdb_id')
            item_type = 'episode' if 'season_number' in item and 'episode_number' in item else 'movie'
            normalized_title = normalize_string(str(item.get('title', 'Unknown')))

            # Check watch history if enabled
            if do_not_add_watched and watch_history_conn:
                if item_type == 'movie':
                    if imdb_id or tmdb_id:
                        query = "SELECT 1 FROM watch_history WHERE type = 'movie' AND "
                        params = []
                        conditions = []
                        if imdb_id: conditions.append("imdb_id = ?"); params.append(imdb_id)
                        if tmdb_id: conditions.append("tmdb_id = ?"); params.append(tmdb_id)
                        if conditions:
                            query += " OR ".join(conditions)
                            if watch_history_conn.execute(query, params).fetchone():
                                skip_stats['already_watched'] += 1; items_skipped += 1; continue
                else:
                    season = item.get('season_number'); episode = item.get('episode_number')
                    show_title = normalized_title
                    if (imdb_id or tmdb_id) and season is not None and episode is not None:
                        query = """SELECT 1 FROM watch_history WHERE type = 'episode' AND season = ? AND episode = ? AND show_title = ? AND ("""
                        params = [season, episode, show_title]
                        conditions = []
                        if imdb_id: conditions.append("imdb_id = ?"); params.append(imdb_id)
                        if tmdb_id: conditions.append("tmdb_id = ?"); params.append(tmdb_id)
                        if conditions:
                           query += " OR ".join(conditions) + ")"
                           if watch_history_conn.execute(query, params).fetchone():
                                skip_stats['already_watched'] += 1; items_skipped += 1; continue

            # Check if any existing version is blacklisted, regardless of granular settings
            is_blacklisted_in_db = False
            if item_type == 'movie':
                existing_versions_states = []
                if imdb_id and imdb_id in existing_movies:
                    existing_versions_states.extend(existing_movies[imdb_id])
                # Avoid double-checking if tmdb_id is the same as imdb_id or if tmdb_id is not in existing_movies
                if tmdb_id and tmdb_id in existing_movies and (not imdb_id or imdb_id != tmdb_id):
                    existing_versions_states.extend(existing_movies[tmdb_id])

                for _, state in existing_versions_states:
                    if state == 'Blacklisted':
                        is_blacklisted_in_db = True
                        break
            else: # Episode
                season_number = item.get('season_number'); episode_number = item.get('episode_number')
                existing_versions_states = []
                imdb_key = None
                tmdb_key = None
                if imdb_id:
                    imdb_key = (str(imdb_id), season_number, episode_number)
                    if imdb_key in existing_episodes:
                        existing_versions_states.extend(existing_episodes[imdb_key])
                if tmdb_id:
                    tmdb_key = (str(tmdb_id), season_number, episode_number)
                    # Avoid double-checking if tmdb_key is the same as imdb_key or if tmdb_key is not in existing_episodes
                    if tmdb_key in existing_episodes and (not imdb_key or imdb_key != tmdb_key):
                         existing_versions_states.extend(existing_episodes[tmdb_key])

                for _, state in existing_versions_states:
                    if state == 'Blacklisted':
                        is_blacklisted_in_db = True
                        break

            if is_blacklisted_in_db:
                skip_stats['existing_blacklisted'] += 1
                items_skipped += 1
                # Optionally log which item was skipped due to blacklist
                # logging.info(f"Skipping {normalized_title} ({item_type}, IMDb: {imdb_id}, TMDb: {tmdb_id}) because an existing version is blacklisted.")
                continue

            # Continue with existing version checks
            if item_type == 'movie':
                skip = False
                media_id = imdb_id or tmdb_id
                existing_versions_set = set()
                existing_states_set = set()

                if imdb_id and imdb_id in existing_movies:
                    for version, state in existing_movies[imdb_id]:
                        existing_versions_set.add(version)
                        existing_states_set.add(state)
                # Avoid double-adding if tmdb_id is the same as imdb_id or if tmdb_id is not in existing_movies
                if tmdb_id and tmdb_id in existing_movies and (not imdb_id or imdb_id != tmdb_id):
                    for version, state in existing_movies[tmdb_id]:
                        existing_versions_set.add(version)
                        existing_states_set.add(state)

                if not enable_granular_versions:
                    if existing_versions_set: # If any version exists
                        skip = True
                        # Increment skip stats based on which IDs were found
                        if imdb_id and imdb_id in existing_movies: skip_stats['existing_movie_imdb'] += 1
                        # Check if tmdb_id exists and is different from imdb_id before incrementing tmdb stat
                        if tmdb_id and tmdb_id in existing_movies and (not imdb_id or imdb_id != tmdb_id): skip_stats['existing_movie_tmdb'] += 1
                        if media_id not in version_summary['movies']:
                            version_summary['movies'][media_id] = {'existing': existing_versions_set, 'added': set(), 'title': normalized_title, 'states': existing_states_set}
                else:
                    # Granular version check - only skip versions that already exist
                    new_versions = {v: enabled for v, enabled in versions.items() if strip_version(v) not in existing_versions_set}
                    if new_versions:
                        if media_id not in version_summary['movies']:
                            version_summary['movies'][media_id] = {'existing': existing_versions_set, 'added': set(new_versions.keys()), 'title': normalized_title, 'states': existing_states_set}
                        else:
                            version_summary['movies'][media_id]['added'].update(new_versions.keys())
                        item['versions_to_add'] = new_versions
                    else: # All requested versions already exist
                        skip = True
                        # Increment general skip stat when all versions exist
                        skip_stats['existing_movie_imdb'] += 1 # Can reuse imdb or add a general 'all_versions_exist' stat
                        if media_id not in version_summary['movies']:
                            version_summary['movies'][media_id] = {'existing': existing_versions_set, 'added': set(), 'title': normalized_title, 'states': existing_states_set}

                if skip:
                    items_skipped += 1
                    continue
            else: # Episode
                season_number = item.get('season_number')
                episode_number = item.get('episode_number')
                skip = False
                media_id = imdb_id or tmdb_id
                episode_key = (media_id, season_number, episode_number) # For summary key

                existing_versions_set = set()
                existing_states_set = set()
                imdb_key = None
                tmdb_key = None

                if imdb_id:
                    imdb_key = (str(imdb_id), season_number, episode_number)
                    if imdb_key in existing_episodes:
                        for version, state in existing_episodes[imdb_key]:
                            existing_versions_set.add(version)
                            existing_states_set.add(state)
                if tmdb_id:
                    tmdb_key = (str(tmdb_id), season_number, episode_number)
                    # Avoid double-adding if tmdb_key is same as imdb_key or not in existing_episodes
                    if tmdb_key in existing_episodes and (not imdb_key or imdb_key != tmdb_key):
                        for version, state in existing_episodes[tmdb_key]:
                            existing_versions_set.add(version)
                            existing_states_set.add(state)

                if not enable_granular_versions:
                    if existing_versions_set: # If any version exists
                        skip = True
                        # Increment skip stats based on which IDs were found
                        if imdb_key and imdb_key in existing_episodes: skip_stats['existing_episode_imdb'] += 1
                        if tmdb_key and tmdb_key in existing_episodes and (not imdb_key or imdb_key != tmdb_key): skip_stats['existing_episode_tmdb'] += 1
                        if episode_key not in version_summary['episodes']:
                            version_summary['episodes'][episode_key] = {'existing': existing_versions_set, 'added': set(), 'title': normalized_title, 'states': existing_states_set}
                else:
                    # Granular version check
                    new_versions = {v: enabled for v, enabled in versions.items() if strip_version(v) not in existing_versions_set}
                    if new_versions:
                        if episode_key not in version_summary['episodes']:
                            version_summary['episodes'][episode_key] = {'existing': existing_versions_set, 'added': set(new_versions.keys()), 'title': normalized_title, 'states': existing_states_set}
                        else:
                            version_summary['episodes'][episode_key]['added'].update(new_versions.keys())
                        item['versions_to_add'] = new_versions
                    else: # All requested versions already exist
                        skip = True
                         # Increment general skip stat when all versions exist
                        skip_stats['existing_episode_imdb'] += 1 # Can reuse imdb or add a general 'all_versions_exist' stat
                        if episode_key not in version_summary['episodes']:
                            version_summary['episodes'][episode_key] = {'existing': existing_versions_set, 'added': set(), 'title': normalized_title, 'states': existing_states_set}

                if skip:
                    items_skipped += 1
                    continue

            filtered_media_items_batch.append(item)

        media_items_batch = filtered_media_items_batch

        for item in media_items_batch:
            if not item.get('imdb_id') and not item.get('tmdb_id'):
                skip_stats['missing_ids'] += 1
                items_skipped += 1
                continue

            # Check for blacklisting, considering season number for TV shows
            season_number = item.get('season_number')
            is_item_blacklisted = (
                is_blacklisted(item.get('imdb_id', ''), season_number) or 
                is_blacklisted(item.get('tmdb_id', ''), season_number)
            )
            if is_item_blacklisted:
                skip_stats['blacklisted'] += 1
                items_skipped += 1
                continue

            if not item.get('tmdb_id'):
                from metadata.metadata import get_tmdb_id_and_media_type
                tmdb_id, media_type = get_tmdb_id_and_media_type(item['imdb_id'])
                if tmdb_id:
                    item['tmdb_id'] = str(tmdb_id)
                else:
                    logging.warning(f"Unable to retrieve tmdb_id for {item.get('title', 'Unknown')} (IMDb ID: {item['imdb_id']})")

            normalized_title = normalize_string(str(item.get('title', 'Unknown')))
            item_type = 'episode' if 'season_number' in item and 'episode_number' in item else 'movie'

            genres = json.dumps(item.get('genres', []))
            item_genres_list = [str(g).lower() for g in item.get('genres', [])] # Get genres as lower-case strings
            is_anime = 'anime' in item_genres_list

            # Use the item-specific versions if they exist, otherwise use the original versions
            versions_to_use = item.get('versions_to_add', versions)
            for version, enabled in versions_to_use.items():
                if not enabled:
                    continue

                # --- Anime Filter Logic ---
                version_config = config.get('Scraping', {}).get('versions', {}).get(version, {})
                anime_mode = version_config.get('anime_filter_mode', 'None')
                
                skip_due_to_anime_filter = False
                if anime_mode == 'Anime Only' and not is_anime:
                    skip_due_to_anime_filter = True
                elif anime_mode == 'Non-Anime Only' and is_anime:
                    skip_due_to_anime_filter = True
                
                if skip_due_to_anime_filter:
                    skip_stats['anime_filter'] += 1
                    # Optionally log the skip:
                    # logging.debug(f"Skipping {normalized_title} ({item_type}) for version '{version}' due to anime filter mode '{anime_mode}' (is_anime: {is_anime})")
                    continue # Skip this version and continue to the next
                # --- End Anime Filter Logic ---

                if item_type == 'movie':
                    early_release_flag = False # Initialize flag
                    imdb_id = item.get('imdb_id')
                    release_date_str = item.get('release_date')
                    check_trakt = False

                    # Check if we should even attempt the Trakt check
                    trakt_early_releases_enabled = get_setting('Scraping', 'trakt_early_releases', False)
                    if trakt_early_releases_enabled and imdb_id:
                        if not release_date_str or release_date_str.lower() == 'unknown':
                            check_trakt = True
                            logging.debug(f"Release date unknown for {normalized_title}, checking Trakt early release.")
                        else:
                            try:
                                release_date = datetime.strptime(release_date_str, '%Y-%m-%d').date()
                                today = datetime.now().date()
                                if release_date >= today:
                                    check_trakt = True
                                    logging.debug(f"Release date {release_date_str} is today or future for {normalized_title}, checking Trakt early release.")
                                else:
                                     logging.debug(f"Release date {release_date_str} is in the past for {normalized_title}, skipping Trakt early release check.")
                            except ValueError:
                                logging.warning(f"Invalid release date format '{release_date_str}' for {normalized_title}, checking Trakt early release.")
                                check_trakt = True

                    # Perform Trakt check if conditions met
                    if check_trakt:
                        logging.info(f"Checking Trakt early release lists for movie: {normalized_title} ({imdb_id})")
                        try:
                            trakt_search_results = fetch_items_from_trakt(f"/search/imdb/{imdb_id}")
                            if trakt_search_results and isinstance(trakt_search_results, list) and len(trakt_search_results) > 0:
                                if 'movie' in trakt_search_results[0] and trakt_search_results[0]['movie'].get('ids', {}).get('trakt'):
                                    trakt_id = str(trakt_search_results[0]['movie']['ids']['trakt'])
                                    logging.debug(f"Found Trakt movie ID {trakt_id} for {imdb_id}")
                                    trakt_lists = fetch_items_from_trakt(f"/movies/{trakt_id}/lists/personal/popular")
                                    if trakt_lists: # Check if lists were fetched successfully
                                        for trakt_list in trakt_lists:
                                            if re.search(r'(latest|new).*?(releases)', trakt_list.get('name', ''), re.IGNORECASE):
                                                logging.info(f"Movie {normalized_title} ({imdb_id}) found in early release list: {trakt_list.get('name')}")
                                                early_release_flag = True
                                                break # Found in a list, no need to check others
                                    else:
                                         logging.warning(f"Failed to fetch Trakt lists for movie {trakt_id}")
                                else:
                                    logging.warning(f"Could not extract Trakt ID from search results for {imdb_id}")
                            else:
                                logging.info(f"No Trakt search results found for {imdb_id}")
                        except Exception as e:
                            logging.error(f"Error checking Trakt early release for {imdb_id}: {str(e)}")
                            skip_stats['trakt_error'] += 1 # Track errors

                    # Original INSERT statement modified
                    conn.execute('''
                        INSERT INTO media_items
                        (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated, version, genres, runtime, country, content_source, content_source_detail, physical_release_date, early_release)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        item.get('imdb_id'), item.get('tmdb_id'), normalized_title, item.get('year'),
                        item.get('release_date'), 'Wanted', 'movie', datetime.now(), version, genres, item.get('runtime'),
                        item.get('country', '').lower(), item.get('content_source'), item.get('content_source_detail'),
                        item.get('physical_release_date'), early_release_flag # Pass the flag here
                    ))
                    items_added += 1
                else:
                    # Check if we need to update the show title for all related records
                    update_show_title(conn, item.get('imdb_id'), item.get('tmdb_id'), item.get('title'))
                    
                    airtime = item.get('airtime') or '19:00'
                    
                    from utilities.settings import get_setting

                    if get_setting('Debug', 'allow_partial_overseerr_requests'):
                        initial_state = 'Wanted' if item.get('is_requested_season', True) else 'Blacklisted'
                    else:
                        initial_state = 'Wanted'
                    blacklisted_date = datetime.now(timezone.utc) if initial_state == 'Blacklisted' else None

                    conn.execute('''
                        INSERT INTO media_items
                        (imdb_id, tmdb_id, title, year, release_date, state, type, season_number, episode_number, 
                         episode_title, last_updated, version, runtime, airtime, genres, country, blacklisted_date,
                         requested_season, content_source, content_source_detail)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        item.get('imdb_id'), item.get('tmdb_id'), normalized_title, item.get('year'),
                        item.get('release_date'), initial_state, 'episode',
                        item['season_number'], item['episode_number'], item.get('episode_title', ''),
                        datetime.now(), version, item.get('runtime'), airtime, genres, item.get('country', '').lower(),
                        blacklisted_date, item.get('requested_season', False), item.get('content_source'), item.get('content_source_detail')
                    ))
                    items_added += 1

        conn.commit()
        
        # Generate skip summary report
        skip_report = []
        if enable_granular_versions:
            skip_report.append("Granular version additions enabled:")
            
            # Movies summary
            if version_summary['movies']:
                skip_report.append("\nMovies:")
                for media_id, info in version_summary['movies'].items():
                    id_type = 'IMDb' if str(media_id).startswith('tt') else 'TMDb'
                    skip_report.append(f"  {info['title']} ({id_type} ID: {media_id}):")
                    if info['existing']:
                        skip_report.append(f"    - Existing versions: {sorted(info['existing'])}")
                    if info['added']:
                        skip_report.append(f"    - Added versions: {sorted(info['added'])}")
                    if not info['added']:
                        skip_report.append("    - No new versions added (all requested versions exist)")
                    if 'Blacklisted' in info.get('states', set()):
                        skip_report.append("    - Note: At least one existing version is Blacklisted (addition was skipped earlier)")
            
            # Episodes summary
            if version_summary['episodes']:
                skip_report.append("\nEpisodes:")
                for (media_id, season, episode), info in version_summary['episodes'].items():
                    id_type = 'IMDb' if str(media_id).startswith('tt') else 'TMDb'
                    skip_report.append(f"  {info['title']} S{season:02d}E{episode:02d} ({id_type} ID: {media_id}):")
                    if info['existing']:
                        skip_report.append(f"    - Existing versions: {sorted(info['existing'])}")
                    if info['added']:
                        skip_report.append(f"    - Added versions: {sorted(info['added'])}")
                    if not info['added']:
                        skip_report.append("    - No new versions added (all requested versions exist)")
                    if 'Blacklisted' in info.get('states', set()):
                        skip_report.append("    - Note: At least one existing version is Blacklisted (addition was skipped earlier)")

        else:
            skipped_movie_count = skip_stats['existing_movie_imdb'] + skip_stats['existing_movie_tmdb']
            if skipped_movie_count > 0:
                skip_report.append(f"- {skipped_movie_count} movies skipped because at least one version already exists")
            skipped_episode_count = skip_stats['existing_episode_imdb'] + skip_stats['existing_episode_tmdb']
            if skipped_episode_count > 0:
                skip_report.append(f"- {skipped_episode_count} episodes skipped because at least one version already exists")

        # Add common skip reasons
        if skip_stats['existing_blacklisted'] > 0:
             skip_report.append(f"\n- {skip_stats['existing_blacklisted']} items skipped because an existing version was blacklisted in the DB")
        if skip_stats['missing_ids'] > 0:
            skip_report.append(f"- {skip_stats['missing_ids']} items skipped due to missing IMDb/TMDb IDs")
        if skip_stats['blacklisted'] > 0:
            skip_report.append(f"- {skip_stats['blacklisted']} items skipped due to blacklist")
        if skip_stats['already_watched'] > 0:
            skip_report.append(f"- {skip_stats['already_watched']} items skipped due to watch history")
        if skip_stats['media_type_mismatch'] > 0:
            skip_report.append(f"- {skip_stats['media_type_mismatch']} items skipped due to media type mismatch")
        if skip_stats['anime_filter'] > 0:
            skip_report.append(f"- {skip_stats['anime_filter']} version additions skipped due to anime filter mode")
        if skip_stats['trakt_error'] > 0:
            skip_report.append(f"- {skip_stats['trakt_error']} items skipped Trakt check due to API errors") # Report Trakt errors
        
        if skip_report:
            logging.info("Wanted items processing complete. Skip summary:\n" + "\n".join(skip_report))
        logging.info(f"Final stats - Added: {items_added}, Updated: {items_updated}, Total Skipped: {items_skipped}")
    except Exception as e:
        logging.error(f"Error adding wanted items: {str(e)}", exc_info=True)
        conn.rollback()
        raise
    finally:
        conn.close()
        if watch_history_conn:
            watch_history_conn.close()


def update_show_title(conn, imdb_id: str = None, tmdb_id: str = None, new_title: str = None) -> bool:
    """
    Update the title of a show and all its episodes in the database if the new title differs from the existing one.
    Related records are determined by matching either imdb_id or tmdb_id. The title will be normalized before updating.
    
    Args:
        conn: Database connection
        imdb_id: IMDb ID of the show
        tmdb_id: TMDB ID of the show
        new_title: New title from metadata
        
    Returns:
        bool: True if title was updated, False otherwise
    """
    if not new_title or (not imdb_id and not tmdb_id):
        return False
    
    normalized_new_title = normalize_string(str(new_title))
    
    # Build query conditions for finding related records
    conditions = []
    params = []
    if imdb_id:
        conditions.append("imdb_id = ?")
        params.append(imdb_id)
    if tmdb_id:
        conditions.append("tmdb_id = ?")
        params.append(tmdb_id)
    
    # Check if title is different
    query = f"""
        SELECT title, COUNT(*) as record_count 
        FROM media_items 
        WHERE ({' OR '.join(conditions)})
        AND type IN ('episode', 'show')
        GROUP BY title
        ORDER BY record_count DESC
        LIMIT 1
    """
    
    row = conn.execute(query, params).fetchone()
    if not row:
        return False
        
    existing_title = row['title']
    if existing_title == normalized_new_title:
        return False
    
    # Update all related records (show and episodes) that share the same imdb_id or tmdb_id
    update_query = f"""
        UPDATE media_items 
        SET title = ?,
            last_updated = ?
        WHERE ({' OR '.join(conditions)})
        AND type IN ('episode', 'show')
    """
    update_params = [normalized_new_title, datetime.now(timezone.utc)] + params # Renamed params to update_params
    conn.execute(update_query, update_params)
    
    logging.info(f"Updated show title from '{existing_title}' to '{normalized_new_title}' for {row['record_count']} records (IMDb: {imdb_id}, TMDb: {tmdb_id})") # Added IDs for clarity
    return True

def process_batch(conn, batch_items, versions, processed):
    """Helper function to process a batch of items"""
    movie_items = []
    episode_items = []
    
    for item, item_type, normalized_title, genres in batch_items:
        if item_type == 'movie':
            for version, enabled in versions.items():
                if enabled:
                    movie_items.append((
                        item.get('imdb_id'), item.get('tmdb_id'), normalized_title, 
                        item.get('year'), item.get('release_date'), 'Wanted', 'movie', 
                        datetime.now(), version, genres, item.get('runtime'), 
                        item.get('country', '').lower(), item.get('content_source'),
                        item.get('content_source_detail'), item.get('physical_release_date')
                    ))
        else:
            for version, enabled in versions.items():
                if enabled:
                    initial_state = 'Wanted' if item.get('is_requested_season', True) else 'Blacklisted'
                    blacklisted_date = datetime.now(timezone.utc) if initial_state == 'Blacklisted' else None
                    
                    episode_items.append((
                        item.get('imdb_id'), item.get('tmdb_id'), normalized_title,
                        item.get('year'), item.get('release_date'), initial_state, 'episode',
                        item['season_number'], item['episode_number'], item.get('episode_title', ''),
                        datetime.now(), version, item.get('runtime'), item.get('airtime', '19:00'),
                        genres, item.get('country', '').lower(), blacklisted_date,
                        item.get('requested_season', False), item.get('content_source'),
                        item.get('content_source_detail')
                    ))
    
    if movie_items:
        conn.executemany('''
            INSERT INTO media_items
            (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated, 
             version, genres, runtime, country, content_source, content_source_detail, physical_release_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', movie_items)
        processed['movies'] += len(movie_items)
    
    if episode_items:
        conn.executemany('''
            INSERT INTO media_items
            (imdb_id, tmdb_id, title, year, release_date, state, type, season_number,
             episode_number, episode_title, last_updated, version, runtime, airtime,
             genres, country, blacklisted_date, requested_season, content_source,
             content_source_detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', episode_items)
        processed['episodes'] += len(episode_items)