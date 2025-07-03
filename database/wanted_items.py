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
            'existing_blacklisted': 0,
            'trakt_error': 0,
            'anime_filter': 0,
            'monitor_mode_no_date': 0,
            'monitor_mode_invalid_date': 0,
            'monitor_mode_future_skip': 0,
            'monitor_mode_recent_skip': 0
        }
        airtime_cache = {}

        config = load_config()
        content_sources = config.get('Content Sources', {})

        do_not_add_watched = get_setting('Debug','do_not_add_plex_watch_history_items_to_queue', False)
        watch_history_conn = None
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
            content_source = item.get('content_source')
            if content_source and content_source in content_sources:
                source_config = content_sources[content_source]
                source_media_type = source_config.get('media_type', 'All')
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

        existing_movies = {}
        batch_size = 450
        
        def strip_version(version):
            return version.rstrip('*') if version else version

        version_summary = {
            'movies': {},
            'episodes': {}
        }

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

        existing_episodes = {}

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

        filtered_media_items_batch_after_existence_check = []
        for item in media_items_batch:
            imdb_id = item.get('imdb_id')
            tmdb_id = item.get('tmdb_id')
            item_type = 'episode' if 'season_number' in item and 'episode_number' in item else 'movie'
            normalized_title = normalize_string(str(item.get('title', 'Unknown')))

            if do_not_add_watched and watch_history_conn:
                if item_type == 'movie':
                    if imdb_id or tmdb_id:
                        query_wh = "SELECT 1 FROM watch_history WHERE type = 'movie' AND "
                        params_wh = []
                        conditions_wh = []
                        if imdb_id: conditions_wh.append("imdb_id = ?"); params_wh.append(imdb_id)
                        if tmdb_id: conditions_wh.append("tmdb_id = ?"); params_wh.append(tmdb_id)
                        if conditions_wh:
                            query_wh += " OR ".join(conditions_wh)
                            if watch_history_conn.execute(query_wh, params_wh).fetchone():
                                skip_stats['already_watched'] += 1; items_skipped += 1; continue
                else:
                    season = item.get('season_number'); episode = item.get('episode_number')
                    show_title_wh = normalized_title
                    if (imdb_id or tmdb_id) and season is not None and episode is not None:
                        query_wh = """SELECT 1 FROM watch_history WHERE type = 'episode' AND season = ? AND episode = ? AND show_title = ? AND ("""
                        params_wh = [season, episode, show_title_wh]
                        conditions_wh = []
                        if imdb_id: conditions_wh.append("imdb_id = ?"); params_wh.append(imdb_id)
                        if tmdb_id: conditions_wh.append("tmdb_id = ?"); params_wh.append(tmdb_id)
                        if conditions_wh:
                           query_wh += " OR ".join(conditions_wh) + ")"
                           if watch_history_conn.execute(query_wh, params_wh).fetchone():
                                skip_stats['already_watched'] += 1; items_skipped += 1; continue
            
            is_blacklisted_in_db = False
            if item_type == 'movie':
                existing_versions_states_check = []
                if imdb_id and imdb_id in existing_movies:
                    existing_versions_states_check.extend(existing_movies[imdb_id])
                if tmdb_id and tmdb_id in existing_movies and (not imdb_id or imdb_id != tmdb_id):
                    existing_versions_states_check.extend(existing_movies[tmdb_id])
                for _, state in existing_versions_states_check:
                    if state == 'Blacklisted':
                        is_blacklisted_in_db = True; break
            else: 
                season_number_check = item.get('season_number'); episode_number_check = item.get('episode_number')
                existing_versions_states_check = []
                imdb_key_check = None; tmdb_key_check = None
                if imdb_id:
                    imdb_key_check = (str(imdb_id), season_number_check, episode_number_check)
                    if imdb_key_check in existing_episodes: existing_versions_states_check.extend(existing_episodes[imdb_key_check])
                if tmdb_id:
                    tmdb_key_check = (str(tmdb_id), season_number_check, episode_number_check)
                    if tmdb_key_check in existing_episodes and (not imdb_key_check or imdb_key_check != tmdb_key_check):
                         existing_versions_states_check.extend(existing_episodes[tmdb_key_check])
                for _, state in existing_versions_states_check:
                    if state == 'Blacklisted':
                        is_blacklisted_in_db = True; break
            
            if is_blacklisted_in_db:
                if not enable_granular_versions:
                    skip_stats['existing_blacklisted'] += 1; items_skipped += 1; continue

            if item_type == 'movie':
                skip = False; media_id_vs = imdb_id or tmdb_id
                existing_versions_set_vs = set(); existing_states_set_vs = set()
                if imdb_id and imdb_id in existing_movies:
                    for version_vs, state_vs in existing_movies[imdb_id]:
                        existing_versions_set_vs.add(version_vs); existing_states_set_vs.add(state_vs)
                if tmdb_id and tmdb_id in existing_movies and (not imdb_id or imdb_id != tmdb_id):
                    for version_vs, state_vs in existing_movies[tmdb_id]:
                        existing_versions_set_vs.add(version_vs); existing_states_set_vs.add(state_vs)

                if not enable_granular_versions:
                    if existing_versions_set_vs:
                        skip = True
                        if imdb_id and imdb_id in existing_movies: skip_stats['existing_movie_imdb'] += 1
                        if tmdb_id and tmdb_id in existing_movies and (not imdb_id or imdb_id != tmdb_id): skip_stats['existing_movie_tmdb'] += 1
                        if media_id_vs not in version_summary['movies']:
                            version_summary['movies'][media_id_vs] = {'existing': existing_versions_set_vs, 'added': set(), 'title': normalized_title, 'states': existing_states_set_vs}
                else:
                    new_versions_vs = {v: enabled for v, enabled in versions.items() if strip_version(v) not in existing_versions_set_vs}
                    if new_versions_vs:
                        if media_id_vs not in version_summary['movies']:
                            version_summary['movies'][media_id_vs] = {'existing': existing_versions_set_vs, 'added': set(new_versions_vs.keys()), 'title': normalized_title, 'states': existing_states_set_vs}
                        else:
                            version_summary['movies'][media_id_vs]['added'].update(new_versions_vs.keys())
                        item['versions_to_add'] = new_versions_vs
                    else:
                        skip = True; skip_stats['existing_movie_imdb'] += 1
                        if media_id_vs not in version_summary['movies']:
                            version_summary['movies'][media_id_vs] = {'existing': existing_versions_set_vs, 'added': set(), 'title': normalized_title, 'states': existing_states_set_vs}
                if skip: items_skipped += 1; continue
            else: # Episode
                season_number_vs = item.get('season_number'); episode_number_vs = item.get('episode_number')
                skip = False; media_id_vs = imdb_id or tmdb_id
                episode_key_vs = (media_id_vs, season_number_vs, episode_number_vs)
                existing_versions_set_vs = set(); existing_states_set_vs = set()
                imdb_key_vs = None; tmdb_key_vs = None
                if imdb_id:
                    imdb_key_vs = (str(imdb_id), season_number_vs, episode_number_vs)
                    if imdb_key_vs in existing_episodes:
                        for version_vs, state_vs in existing_episodes[imdb_key_vs]:
                            existing_versions_set_vs.add(version_vs); existing_states_set_vs.add(state_vs)
                if tmdb_id:
                    tmdb_key_vs = (str(tmdb_id), season_number_vs, episode_number_vs)
                    if tmdb_key_vs in existing_episodes and (not imdb_key_vs or imdb_key_vs != tmdb_key_vs):
                        for version_vs, state_vs in existing_episodes[tmdb_key_vs]:
                            existing_versions_set_vs.add(version_vs); existing_states_set_vs.add(state_vs)

                if not enable_granular_versions:
                    if existing_versions_set_vs:
                        skip = True
                        if imdb_key_vs and imdb_key_vs in existing_episodes: skip_stats['existing_episode_imdb'] += 1
                        if tmdb_key_vs and tmdb_key_vs in existing_episodes and (not imdb_key_vs or imdb_key_vs != tmdb_key_vs): skip_stats['existing_episode_tmdb'] += 1
                        if episode_key_vs not in version_summary['episodes']:
                            version_summary['episodes'][episode_key_vs] = {'existing': existing_versions_set_vs, 'added': set(), 'title': normalized_title, 'states': existing_states_set_vs}
                else:
                    new_versions_vs = {v: enabled for v, enabled in versions.items() if strip_version(v) not in existing_versions_set_vs}
                    if new_versions_vs:
                        if episode_key_vs not in version_summary['episodes']:
                            version_summary['episodes'][episode_key_vs] = {'existing': existing_versions_set_vs, 'added': set(new_versions_vs.keys()), 'title': normalized_title, 'states': existing_states_set_vs}
                        else:
                            version_summary['episodes'][episode_key_vs]['added'].update(new_versions_vs.keys())
                        item['versions_to_add'] = new_versions_vs
                    else:
                        skip = True; skip_stats['existing_episode_imdb'] += 1
                        if episode_key_vs not in version_summary['episodes']:
                             version_summary['episodes'][episode_key_vs] = {'existing': existing_versions_set_vs, 'added': set(), 'title': normalized_title, 'states': existing_states_set_vs}
                if skip: items_skipped += 1; continue
            
            filtered_media_items_batch_after_existence_check.append(item)

        media_items_batch = filtered_media_items_batch_after_existence_check
        
        movies_to_insert = []
        episodes_to_insert = []
        show_titles_to_potentially_update = set()

        for item in media_items_batch:
            if not item.get('imdb_id') and not item.get('tmdb_id'):
                skip_stats['missing_ids'] += 1
                items_skipped += 1
                continue

            season_number_for_blacklist = item.get('season_number')
            is_item_blacklisted = (
                is_blacklisted(item.get('imdb_id', ''), season_number_for_blacklist) or 
                is_blacklisted(item.get('tmdb_id', ''), season_number_for_blacklist)
            )
            if is_item_blacklisted:
                skip_stats['blacklisted'] += 1
                items_skipped += 1
                continue

            if not item.get('tmdb_id'):
                from metadata.metadata import get_tmdb_id_and_media_type
                tmdb_id_meta, media_type_meta = get_tmdb_id_and_media_type(item['imdb_id'])
                if tmdb_id_meta:
                    item['tmdb_id'] = str(tmdb_id_meta)
                else:
                    logging.warning(f"Unable to retrieve tmdb_id for {item.get('title', 'Unknown')} (IMDb ID: {item['imdb_id']})")

            normalized_title = normalize_string(str(item.get('title', 'Unknown')))
            item_type = 'episode' if 'season_number' in item and 'episode_number' in item else 'movie'
            content_source = item.get('content_source')

            # Monitor Mode Filtering for "Collected_" sources (episodes only)
            if item_type == 'episode' and content_source and content_source.startswith('Collected_'):
                if content_source in content_sources:
                    source_config = content_sources[content_source]
                    monitor_mode = source_config.get('monitor_mode', 'Monitor All Episodes')
                    
                    if monitor_mode != 'Monitor All Episodes':
                        release_date_str = item.get('release_date')

                        if not release_date_str:
                            logging.warning(f"MONITOR_MODE_SKIP (Missing Date): Episode '{normalized_title}' from source '{content_source}'. monitor_mode: {monitor_mode}.")
                            skip_stats.setdefault('monitor_mode_no_date', 0)
                            skip_stats['monitor_mode_no_date'] += 1
                            items_skipped += 1
                            continue 

                        try:
                            release_date_obj = datetime.strptime(release_date_str, '%Y-%m-%d').date()
                            today = datetime.now().date()

                            if monitor_mode == 'Monitor Future Episodes':
                                if release_date_obj < today:
                                    skip_stats.setdefault('monitor_mode_future_skip', 0)
                                    skip_stats['monitor_mode_future_skip'] += 1
                                    items_skipped += 1
                                    continue
                            elif monitor_mode == 'Monitor Recent (90 Days) and Future':
                                ninety_days_ago = today - timedelta(days=90)
                                if release_date_obj < ninety_days_ago:
                                    skip_stats.setdefault('monitor_mode_recent_skip', 0)
                                    skip_stats['monitor_mode_recent_skip'] += 1
                                    items_skipped += 1
                                    continue
                        except ValueError:
                            logging.warning(f"MONITOR_MODE_SKIP (Invalid Date Format): Episode '{normalized_title}' from source '{content_source}' due to invalid release date format '{release_date_str}'. monitor_mode: {monitor_mode}.")
                            skip_stats.setdefault('monitor_mode_invalid_date', 0)
                            skip_stats['monitor_mode_invalid_date'] += 1
                            items_skipped += 1
                            continue
                else:
                    logging.warning(f"Content source '{content_source}' for item '{normalized_title}' not found in configuration. Skipping monitor_mode check for this item.")
            
            genres = json.dumps(item.get('genres', []))
            item_genres_list = [str(g).lower() for g in item.get('genres', [])]
            is_anime = 'anime' in item_genres_list
            versions_to_use = item.get('versions_to_add', versions)

            for version, enabled in versions_to_use.items():
                if not enabled:
                    continue

                version_config = config.get('Scraping', {}).get('versions', {}).get(version, {})
                anime_mode = version_config.get('anime_filter_mode', 'None')
                skip_due_to_anime_filter = False
                if anime_mode == 'Anime Only' and not is_anime:
                    skip_due_to_anime_filter = True
                elif anime_mode == 'Non-Anime Only' and is_anime:
                    skip_due_to_anime_filter = True
                if skip_due_to_anime_filter:
                    skip_stats['anime_filter'] += 1
                    continue

                if item_type == 'movie':
                    early_release_flag = False
                    imdb_id = item.get('imdb_id')
                    release_date_str = item.get('release_date')
                    check_trakt = False
                    trakt_early_releases_enabled = get_setting('Scraping', 'trakt_early_releases', False)

                    if trakt_early_releases_enabled and imdb_id:
                        if not release_date_str or release_date_str.lower() == 'unknown':
                            check_trakt = True
                        else:
                            try:
                                release_date = datetime.strptime(release_date_str, '%Y-%m-%d').date()
                                if release_date >= datetime.now().date():
                                    check_trakt = True
                            except ValueError:
                                check_trakt = True
                    
                    if check_trakt:
                        logging.info(f"Checking Trakt early release lists for movie: {normalized_title} ({imdb_id})")
                        try:
                            trakt_search_results = fetch_items_from_trakt(f"/search/imdb/{imdb_id}")
                            if trakt_search_results and isinstance(trakt_search_results, list) and len(trakt_search_results) > 0:
                                if 'movie' in trakt_search_results[0] and trakt_search_results[0]['movie'].get('ids', {}).get('trakt'):
                                    trakt_id = str(trakt_search_results[0]['movie']['ids']['trakt'])
                                    trakt_lists = fetch_items_from_trakt(f"/movies/{trakt_id}/lists/personal/popular")
                                    if trakt_lists:
                                        for trakt_list in trakt_lists:
                                            if re.search(r'(latest|new).*?(releases)', trakt_list.get('name', ''), re.IGNORECASE):
                                                early_release_flag = True; break
                                    else: logging.warning(f"Failed to fetch Trakt lists for movie {trakt_id}")
                                else: logging.warning(f"Could not extract Trakt ID from search results for {imdb_id}")
                            else: logging.info(f"No Trakt search results found for {imdb_id}")
                        except Exception as e:
                            logging.error(f"Error checking Trakt early release for {imdb_id}: {str(e)}")
                            skip_stats['trakt_error'] += 1
                    
                    movie_data = (
                        item.get('imdb_id'), item.get('tmdb_id'), normalized_title, item.get('year'),
                        item.get('release_date'), 'Wanted', 'movie', datetime.now(), version, genres, item.get('runtime'),
                        item.get('country', '').lower(), item.get('content_source'), item.get('content_source_detail'),
                        item.get('physical_release_date'), early_release_flag
                    )
                    movies_to_insert.append(movie_data)
                    items_added += 1
                else: # episode
                    if item.get('imdb_id') or item.get('tmdb_id'):
                        show_titles_to_potentially_update.add(
                            (item.get('imdb_id'), item.get('tmdb_id'), item.get('title'))
                        )
                    
                    airtime = item.get('airtime') or '19:00'
                    initial_state = 'Wanted'
                    if get_setting('Debug', 'allow_partial_overseerr_requests'):
                         initial_state = 'Wanted' if item.get('is_requested_season', True) else 'Blacklisted'
                    blacklisted_date = datetime.now(timezone.utc) if initial_state == 'Blacklisted' else None

                    episode_data = (
                        item.get('imdb_id'), item.get('tmdb_id'), normalized_title, item.get('year'),
                        item.get('release_date'), initial_state, 'episode',
                        item['season_number'], item['episode_number'], item.get('episode_title', ''),
                        datetime.now(), version, item.get('runtime'), airtime, genres, item.get('country', '').lower(),
                        blacklisted_date, item.get('requested_season', False), item.get('content_source'), item.get('content_source_detail')
                    )
                    episodes_to_insert.append(episode_data)
                    items_added += 1
        
        # Perform deferred show title updates
        updated_any_title = False
        if show_titles_to_potentially_update:
            logging.debug(f"Processing {len(show_titles_to_potentially_update)} unique show title update candidates.")
            for imdb_id_s, tmdb_id_s, new_title_s in show_titles_to_potentially_update:
                if update_show_title(conn, imdb_id_s, tmdb_id_s, new_title_s):
                    updated_any_title = True
        
        # Perform batch inserts
        if movies_to_insert:
            conn.executemany('''
                INSERT INTO media_items
                (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated, version, genres, runtime, country, content_source, content_source_detail, physical_release_date, early_release)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', movies_to_insert)

        if episodes_to_insert:
            conn.executemany('''
                INSERT INTO media_items
                (imdb_id, tmdb_id, title, year, release_date, state, type, season_number, episode_number, 
                 episode_title, last_updated, version, runtime, airtime, genres, country, blacklisted_date,
                 requested_season, content_source, content_source_detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', episodes_to_insert)

        if movies_to_insert or episodes_to_insert or updated_any_title:
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
        
        # Add new monitor_mode skip reasons to the report
        if skip_stats.get('monitor_mode_future_skip', 0) > 0:
            skip_report.append(f"- {skip_stats['monitor_mode_future_skip']} episodes skipped by 'Monitor Future Episodes' mode")
        if skip_stats.get('monitor_mode_recent_skip', 0) > 0:
            skip_report.append(f"- {skip_stats['monitor_mode_recent_skip']} episodes skipped by 'Monitor Recent (90 Days) and Future' mode")
        if skip_stats.get('monitor_mode_no_date', 0) > 0:
            skip_report.append(f"- {skip_stats['monitor_mode_no_date']} episodes skipped by monitor mode due to missing release date")
        if skip_stats.get('monitor_mode_invalid_date', 0) > 0:
            skip_report.append(f"- {skip_stats['monitor_mode_invalid_date']} episodes skipped by monitor mode due to invalid release date format")

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