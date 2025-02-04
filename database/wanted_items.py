import logging
from .core import get_db_connection, normalize_string, get_existing_airtime
from manual_blacklist import is_blacklisted
from typing import List, Dict, Any
import json
from datetime import datetime, timezone
from metadata.metadata import get_tmdb_id_and_media_type
import random
import os
from config_manager import load_config

def add_wanted_items(media_items_batch: List[Dict[str, Any]], versions_input):
    from metadata.metadata import get_show_airtime_by_imdb_id
    from settings import get_setting

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
            'media_type_mismatch': 0
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

            # Check watch history if enabled
            if do_not_add_watched and watch_history_conn:
                if item_type == 'movie':
                    # Check if movie exists in watch history
                    if imdb_id or tmdb_id:
                        query = "SELECT 1 FROM watch_history WHERE type = 'movie' AND "
                        params = []
                        conditions = []
                        if imdb_id:
                            conditions.append("imdb_id = ?")
                            params.append(imdb_id)
                        if tmdb_id:
                            conditions.append("tmdb_id = ?")
                            params.append(tmdb_id)
                        query += " OR ".join(conditions)
                        if watch_history_conn.execute(query, params).fetchone():
                            skip_stats['already_watched'] += 1
                            items_skipped += 1
                            continue
                else:
                    # Check if episode exists in watch history
                    season = item.get('season_number')
                    episode = item.get('episode_number')
                    if (imdb_id or tmdb_id) and season is not None and episode is not None:
                        query = """
                            SELECT 1 FROM watch_history 
                            WHERE type = 'episode' 
                            AND season = ? 
                            AND episode = ? 
                            AND (
                        """
                        params = [season, episode]
                        conditions = []
                        if imdb_id:
                            conditions.append("imdb_id = ?")
                            params.append(imdb_id)
                        if tmdb_id:
                            conditions.append("tmdb_id = ?")
                            params.append(tmdb_id)
                        query += " OR ".join(conditions) + ")"
                        if watch_history_conn.execute(query, params).fetchone():
                            skip_stats['already_watched'] += 1
                            items_skipped += 1
                            continue

            # Continue with existing checks
            if item_type == 'movie':
                skip = False
                if imdb_id and imdb_id in existing_movies:
                    skip_stats['existing_movie_imdb'] += 1
                    skip = True
                if tmdb_id and tmdb_id in existing_movies:
                    skip_stats['existing_movie_tmdb'] += 1
                    skip = True
                if skip:
                    items_skipped += 1
                    continue
            else:
                season_number = item.get('season_number')
                episode_number = item.get('episode_number')
                skip = False
                if imdb_id and (imdb_id, season_number, episode_number) in existing_episodes:
                    skip_stats['existing_episode_imdb'] += 1
                    skip = True
                if tmdb_id and (tmdb_id, season_number, episode_number) in existing_episodes:
                    skip_stats['existing_episode_tmdb'] += 1
                    skip = True
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
                        (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated, version, genres, runtime, country, content_source)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        item.get('imdb_id'), item.get('tmdb_id'), normalized_title, item.get('year'),
                        item.get('release_date'), 'Wanted', 'movie', datetime.now(), version, genres, item.get('runtime'), item.get('country', '').lower(), item.get('content_source')
                    ))
                    items_added += 1
                else:
                    # Check if we need to update the show title for all related records
                    update_show_title(conn, item.get('imdb_id'), item.get('tmdb_id'), item.get('title'))
                    
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
                         requested_season, content_source)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        item.get('imdb_id'), item.get('tmdb_id'), normalized_title, item.get('year'),
                        item.get('release_date'), initial_state, 'episode',
                        item['season_number'], item['episode_number'], item.get('episode_title', ''),
                        datetime.now(), version, item.get('runtime'), airtime, genres, item.get('country', '').lower(),
                        blacklisted_date, item.get('requested_season', False), item.get('content_source')
                    ))
                    items_added += 1

        conn.commit()
        
        # Generate skip summary report
        skip_report = []
        if skip_stats['existing_movie_imdb'] > 0:
            skip_report.append(f"- {skip_stats['existing_movie_imdb']} movies already exist (by IMDb ID)")
        if skip_stats['existing_movie_tmdb'] > 0:
            skip_report.append(f"- {skip_stats['existing_movie_tmdb']} movies already exist (by TMDb ID)")
        if skip_stats['existing_episode_imdb'] > 0:
            skip_report.append(f"- {skip_stats['existing_episode_imdb']} episodes already exist (by IMDb ID)")
        if skip_stats['existing_episode_tmdb'] > 0:
            skip_report.append(f"- {skip_stats['existing_episode_tmdb']} episodes already exist (by TMDb ID)")
        if skip_stats['missing_ids'] > 0:
            skip_report.append(f"- {skip_stats['missing_ids']} items skipped due to missing IMDb/TMDb IDs")
        if skip_stats['blacklisted'] > 0:
            skip_report.append(f"- {skip_stats['blacklisted']} items skipped due to blacklist")
        if skip_stats['already_watched'] > 0:
            skip_report.append(f"- {skip_stats['already_watched']} items skipped due to watch history")
        if skip_stats['media_type_mismatch'] > 0:
            skip_report.append(f"- {skip_stats['media_type_mismatch']} items skipped due to media type mismatch")
        
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
    params = [normalized_new_title, datetime.now(timezone.utc)] + params
    conn.execute(update_query, params)
    conn.commit()
    
    logging.info(f"Updated show title from '{existing_title}' to '{normalized_new_title}' for {row['record_count']} records")
    return True