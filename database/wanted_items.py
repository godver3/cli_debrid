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
from content_checkers.trakt import fetch_items_from_trakt, load_imdb_trakt_cache, save_imdb_trakt_cache
import re

def _get_existing_item_id_collected(conn, imdb_id, tmdb_id, item_type, item):
    """Get DB id of an existing Collected/Upgrading item matching the given identifiers."""
    if item_type == 'movie':
        for id_col, id_val in [('imdb_id', imdb_id), ('tmdb_id', tmdb_id)]:
            if id_val:
                row = conn.execute(
                    f"SELECT id FROM media_items WHERE type='movie' AND {id_col}=? AND state IN ('Collected','Upgrading') LIMIT 1",
                    (id_val,)
                ).fetchone()
                if row:
                    return row['id']
    else:
        season = item.get('season_number')
        episode = item.get('episode_number')
        for id_col, id_val in [('imdb_id', imdb_id), ('tmdb_id', tmdb_id)]:
            if id_val:
                row = conn.execute(
                    f"SELECT id FROM media_items WHERE type='episode' AND {id_col}=? AND season_number=? AND episode_number=? AND state IN ('Collected','Upgrading') LIMIT 1",
                    (id_val, season, episode)
                ).fetchone()
                if row:
                    return row['id']
    return None


def _get_existing_item_id_any_state(conn, imdb_id, tmdb_id, item_type, item):
    """Get DB id of any existing item matching the given identifiers (any state)."""
    if item_type == 'movie':
        for id_col, id_val in [('imdb_id', imdb_id), ('tmdb_id', tmdb_id)]:
            if id_val:
                row = conn.execute(
                    f"SELECT id FROM media_items WHERE type='movie' AND {id_col}=? LIMIT 1",
                    (id_val,)
                ).fetchone()
                if row:
                    return row['id']
    else:
        season = item.get('season_number')
        episode = item.get('episode_number')
        for id_col, id_val in [('imdb_id', imdb_id), ('tmdb_id', tmdb_id)]:
            if id_val:
                row = conn.execute(
                    f"SELECT id FROM media_items WHERE type='episode' AND {id_col}=? AND season_number=? AND episode_number=? LIMIT 1",
                    (id_val, season, episode)
                ).fetchone()
                if row:
                    return row['id']
    return None


def add_wanted_items(media_items_batch: List[Dict[str, Any]], versions_input, unblacklist: bool = False, force_granular_versions: bool = False):
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
            'already_collected_or_upgrading': 0,
            'trakt_error': 0,
            'anime_filter': 0,
            'monitor_mode_no_date': 0,
            'monitor_mode_invalid_date': 0,
            'monitor_mode_future_skip': 0,
            'monitor_mode_recent_skip': 0
        }
        airtime_cache = {}

        # Load IMDB→Trakt ID cache to avoid redundant API calls
        imdb_trakt_cache = load_imdb_trakt_cache()
        cache_hits = 0
        cache_misses = 0

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

        # CRITICAL FIX: Deduplicate incoming batch to prevent multiple entries for same movie
        # If the batch contains multiple items with the same IMDb ID and version, keep only the first one
        seen_items = {}  # Key: (imdb_id or tmdb_id, version, type), Value: first item with this key
        deduplicated_batch = []
        duplicates_in_batch = 0

        for item in media_items_batch:
            imdb_id = item.get('imdb_id')
            tmdb_id = item.get('tmdb_id')
            version = item.get('version', 'Default')
            item_type = 'episode' if 'season_number' in item and 'episode_number' in item else 'movie'

            # For episodes, include season/episode in the key
            if item_type == 'episode':
                season = item.get('season_number')
                episode = item.get('episode_number')
                # Use IMDb ID if available, otherwise TMDB ID
                identifier = imdb_id if imdb_id else tmdb_id
                key = (identifier, version, item_type, season, episode)
            else:
                # For movies, use IMDb ID if available, otherwise TMDB ID
                identifier = imdb_id if imdb_id else tmdb_id
                key = (identifier, version, item_type)

            if key in seen_items:
                # Duplicate found in batch - skip it
                duplicates_in_batch += 1
                logging.debug(f"Skipping duplicate in batch: {item.get('title')} ({identifier}, version={version})")
            else:
                # First occurrence - keep it
                seen_items[key] = item
                deduplicated_batch.append(item)

        if duplicates_in_batch > 0:
            logging.info(f"⚠️  Removed {duplicates_in_batch} duplicate items from incoming batch before processing")

        media_items_batch = deduplicated_batch

        existing_movies = {}
        batch_size = 450
        
        def strip_version(version):
            return version.rstrip('*') if version else version

        version_summary = {
            'movies': {},
            'episodes': {}
        }

        enable_granular_versions = force_granular_versions or get_setting('Debug', 'enable_granular_version_additions', False)

        if movie_imdb_ids:
            movie_imdb_list = list(movie_imdb_ids)
            for i in range(0, len(movie_imdb_list), batch_size):
                batch = movie_imdb_list[i:i + batch_size]
                placeholders = ', '.join(['?'] * len(batch))
                query = f'''
                    SELECT imdb_id, version, state, ghostlisted FROM media_items
                    WHERE type = 'movie' AND imdb_id IN ({placeholders})
                '''
                rows = conn.execute(query, tuple(batch)).fetchall()
                for row in rows:
                    movie_id = str(row['imdb_id'])
                    if movie_id not in existing_movies:
                        existing_movies[movie_id] = []
                    existing_movies[movie_id].append((strip_version(row['version']), row['state'], row['ghostlisted']))

        if movie_tmdb_ids:
            movie_tmdb_list = list(movie_tmdb_ids)
            for i in range(0, len(movie_tmdb_list), batch_size):
                batch = movie_tmdb_list[i:i + batch_size]
                placeholders = ', '.join(['?'] * len(batch))
                query = f'''
                    SELECT tmdb_id, version, state, ghostlisted FROM media_items
                    WHERE type = 'movie' AND tmdb_id IN ({placeholders})
                '''
                rows = conn.execute(query, tuple(batch)).fetchall()
                for row in rows:
                    movie_id = str(row['tmdb_id'])
                    if movie_id not in existing_movies:
                        existing_movies[movie_id] = []
                    existing_movies[movie_id].append((strip_version(row['version']), row['state'], row['ghostlisted']))

        existing_episodes = {}

        if episode_imdb_ids:
            episode_imdb_list = list(episode_imdb_ids)
            for i in range(0, len(episode_imdb_list), batch_size):
                batch = episode_imdb_list[i:i + batch_size]
                placeholders = ', '.join(['?'] * len(batch))
                query = f'''
                    SELECT imdb_id, season_number, episode_number, version, state, ghostlisted, filled_by_torrent_id FROM media_items
                    WHERE type = 'episode' AND imdb_id IN ({placeholders})
                '''
                rows = conn.execute(query, tuple(batch)).fetchall()
                for row in rows:
                    key = (str(row['imdb_id']), row['season_number'], row['episode_number'])
                    if key not in existing_episodes:
                        existing_episodes[key] = []
                    existing_episodes[key].append((strip_version(row['version']), row['state'], row['ghostlisted'], row['filled_by_torrent_id']))

        if episode_tmdb_ids:
            episode_tmdb_list = list(episode_tmdb_ids)
            for i in range(0, len(episode_tmdb_list), batch_size):
                batch = episode_tmdb_list[i:i + batch_size]
                placeholders = ', '.join(['?'] * len(batch))
                query = f'''
                    SELECT tmdb_id, season_number, episode_number, version, state, ghostlisted, filled_by_torrent_id FROM media_items
                    WHERE type = 'episode' AND tmdb_id IN ({placeholders})
                '''
                rows = conn.execute(query, tuple(batch)).fetchall()
                for row in rows:
                    key = (str(row['tmdb_id']), row['season_number'], row['episode_number'])
                    if key not in existing_episodes:
                        existing_episodes[key] = []
                    existing_episodes[key].append((strip_version(row['version']), row['state'], row['ghostlisted'], row['filled_by_torrent_id']))

        # Check plex_labels once before the loop — avoids repeated config lookups
        try:
            from utilities.plex_label_manager import is_plex_labels_enabled_anywhere as _plex_enabled_check
            _plex_labels_active = _plex_enabled_check()
        except Exception:
            _plex_labels_active = False

        pending_secondary_labels = []  # [(existing_id, source_name, source_detail)] — Collected items needing label from second source
        pending_source_records = []   # [(existing_id, source_name, source_detail)] — not-yet-Collected items needing source recorded

        # Build O(1) lookup: set of (imdb_id, season_number) pairs that are covered by a
        # genuine single season-pack download — i.e. every Collected/Upgrading episode
        # seen for that season shares the SAME filled_by_torrent_id, and there are at
        # least 2 such episodes (one download containing multiple episodes). A season
        # built from several individually-downloaded episodes (each its own
        # filled_by_torrent_id) is NOT included here — see usage below for why.
        _season_pack_torrent_ids = {}  # (imdb_id, season) -> set of distinct filled_by_torrent_id seen
        _season_pack_episode_counts = {}  # (imdb_id, season) -> count of Collected/Upgrading episodes seen
        for (eid, seas, _epnum), vs in existing_episodes.items():
            for _, st, _, filled_by_torrent_id in vs:
                if st in ('Collected', 'Upgrading'):
                    season_key = (eid, seas)
                    _season_pack_torrent_ids.setdefault(season_key, set()).add(filled_by_torrent_id)
                    _season_pack_episode_counts[season_key] = _season_pack_episode_counts.get(season_key, 0) + 1
                    break

        _collected_seasons = {
            season_key for season_key, torrent_ids in _season_pack_torrent_ids.items()
            if len(torrent_ids) == 1 and None not in torrent_ids and _season_pack_episode_counts[season_key] >= 2
        }

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
                    # TIERED FALLBACK: Check watch history with IMDb, then TMDb, then show_title
                    # This prevents cross-ID matching and handles title mismatches
                    season = item.get('season_number')
                    episode = item.get('episode_number')
                    show_title_wh = normalized_title

                    if season is not None and episode is not None:
                        is_watched = False

                        # Tier 1: IMDb ID + season + episode (most reliable)
                        if imdb_id:
                            query_wh = """SELECT 1 FROM watch_history
                                         WHERE type = 'episode' AND season = ? AND episode = ? AND imdb_id = ?"""
                            if watch_history_conn.execute(query_wh, [season, episode, imdb_id]).fetchone():
                                is_watched = True

                        # Tier 2: TMDb ID + season + episode (fallback if no IMDb or not found)
                        if not is_watched and tmdb_id:
                            query_wh = """SELECT 1 FROM watch_history
                                         WHERE type = 'episode' AND season = ? AND episode = ? AND tmdb_id = ?"""
                            if watch_history_conn.execute(query_wh, [season, episode, tmdb_id]).fetchone():
                                is_watched = True

                        # Tier 3: show_title + season + episode (final fallback)
                        if not is_watched and show_title_wh:
                            query_wh = """SELECT 1 FROM watch_history
                                         WHERE type = 'episode' AND season = ? AND episode = ? AND show_title = ?"""
                            if watch_history_conn.execute(query_wh, [season, episode, show_title_wh]).fetchone():
                                is_watched = True

                        if is_watched:
                            skip_stats['already_watched'] += 1
                            items_skipped += 1
                            continue
            
            is_blacklisted_in_db = False
            is_ghostlisted_in_db = False
            if item_type == 'movie':
                existing_versions_states_check = []
                if imdb_id and imdb_id in existing_movies:
                    existing_versions_states_check.extend(existing_movies[imdb_id])
                if tmdb_id and tmdb_id in existing_movies and (not imdb_id or imdb_id != tmdb_id):
                    existing_versions_states_check.extend(existing_movies[tmdb_id])
                for _, state, ghostlisted in existing_versions_states_check:
                    if ghostlisted == 1:
                        is_ghostlisted_in_db = True; is_blacklisted_in_db = True
                    elif state == 'Blacklisted':
                        is_blacklisted_in_db = True
                    if is_ghostlisted_in_db:
                        break
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
                for _, state, ghostlisted, _ in existing_versions_states_check:
                    if ghostlisted == 1:
                        is_ghostlisted_in_db = True; is_blacklisted_in_db = True
                    elif state == 'Blacklisted':
                        is_blacklisted_in_db = True
                    if is_ghostlisted_in_db:
                        break

            if is_blacklisted_in_db:
                if not enable_granular_versions:
                    # If unblacklist is enabled and item is only blacklisted (not ghostlisted), reset it
                    if unblacklist and not is_ghostlisted_in_db:
                        db_item_id = _get_existing_item_id_any_state(conn, imdb_id, tmdb_id, item_type, item)
                        if db_item_id:
                            conn.execute(
                                "UPDATE media_items SET state='Wanted', blacklisted_date=NULL, sleep_cycles=0 WHERE id=?",
                                (db_item_id,)
                            )
                            conn.commit()
                            logging.info(f"Unblacklisted item id={db_item_id} ({item.get('title', 'Unknown')}) per source unblacklist setting")
                            # Allow item to proceed — do not skip
                    else:
                        skip_stats['existing_blacklisted'] += 1; items_skipped += 1; continue

            # Check if item is already Collected or Upgrading (prevent duplicate re-addition)
            is_collected_or_upgrading_in_db = False
            if item_type == 'movie':
                existing_versions_states_check_collected = []
                if imdb_id and imdb_id in existing_movies:
                    existing_versions_states_check_collected.extend(existing_movies[imdb_id])
                if tmdb_id and tmdb_id in existing_movies and (not imdb_id or imdb_id != tmdb_id):
                    existing_versions_states_check_collected.extend(existing_movies[tmdb_id])
                for _, state, _ in existing_versions_states_check_collected:
                    if state in ('Collected', 'Upgrading'):
                        is_collected_or_upgrading_in_db = True; break
            else:
                season_number_check_collected = item.get('season_number'); episode_number_check_collected = item.get('episode_number')
                existing_versions_states_check_collected = []
                imdb_key_check_collected = None; tmdb_key_check_collected = None
                if imdb_id:
                    imdb_key_check_collected = (str(imdb_id), season_number_check_collected, episode_number_check_collected)
                    if imdb_key_check_collected in existing_episodes: existing_versions_states_check_collected.extend(existing_episodes[imdb_key_check_collected])
                if tmdb_id:
                    tmdb_key_check_collected = (str(tmdb_id), season_number_check_collected, episode_number_check_collected)
                    if tmdb_key_check_collected in existing_episodes and (not imdb_key_check_collected or imdb_key_check_collected != tmdb_key_check_collected):
                         existing_versions_states_check_collected.extend(existing_episodes[tmdb_key_check_collected])
                for _, state, _, _ in existing_versions_states_check_collected:
                    if state in ('Collected', 'Upgrading'):
                        is_collected_or_upgrading_in_db = True; break

                # If no per-episode entry found, check if a genuine season-pack file
                # already covers this episode. Only trust this when every Collected/
                # Upgrading episode we've seen for this season shares the SAME
                # filled_by_torrent_id — that's the actual signature of one pack
                # download containing multiple episodes. "Any sibling is Collected"
                # alone is not proof: a partially-collected season built from several
                # individually-downloaded episodes (each its own filled_by_torrent_id)
                # previously got misread as "season already fully covered," silently
                # skipping real gaps (e.g. missing episodes never re-requested).
                if not is_collected_or_upgrading_in_db and imdb_id and season_number_check_collected is not None:
                    if (str(imdb_id), season_number_check_collected) in _collected_seasons:
                        is_collected_or_upgrading_in_db = True

            # User-initiated adds (Request button / manual magnet assign) must always
            # be allowed to scrape and collect their own copy, even if this
            # imdb/tmdb+season+episode already has a Collected/Upgrading entry —
            # the user is explicitly asking for an additional/replacement file, not
            # a background re-sync of a source that already covers this content.
            # Automated content sources (Trakt, Overseerr, Plex Watchlist, etc.)
            # keep the original dedup-skip behavior unchanged.
            is_user_initiated_add = item.get('content_source') in ('content_requester', 'Magnet_Assigner')

            if is_collected_or_upgrading_in_db and not is_user_initiated_add:
                if not enable_granular_versions:
                    if _plex_labels_active:
                        new_source = item.get('content_source')
                        new_detail = item.get('content_source_detail')
                        if new_source and new_detail and new_detail.lower() != 'unknown':
                            lookup_id = _get_existing_item_id_collected(conn, imdb_id, tmdb_id, item_type, item)
                            if lookup_id:
                                # Skip re-queuing if this source already has its label(s) applied —
                                # without this check, the same handful of items get re-labelled
                                # (a no-op write) on every wanted-items pass, needlessly.
                                already_labelled = False
                                try:
                                    from utilities.plex_label_manager import parse_plex_labels, determine_labels_for_item
                                    pl_row = conn.execute('SELECT plex_labels FROM media_items WHERE id = ?', (lookup_id,)).fetchone()
                                    existing_plex_labels = parse_plex_labels(pl_row['plex_labels'] if pl_row else None)
                                    expected_labels = determine_labels_for_item({'content_source': new_source, 'content_source_detail': new_detail})
                                    already_labelled = bool(expected_labels) and all(
                                        new_source in existing_plex_labels.get(lbl, {}).get('sources', [])
                                        for lbl in expected_labels
                                    )
                                except Exception:
                                    already_labelled = False
                                if not already_labelled:
                                    pending_secondary_labels.append((lookup_id, new_source, new_detail))
                                # Write source+detail to content_sources so secondary-source label
                                # re-processing can find it (add_label_to_item only writes source, no detail)
                                try:
                                    from utilities.plex_label_manager import parse_content_sources, serialize_content_sources
                                    cs_row = conn.execute('SELECT content_sources FROM media_items WHERE id = ?', (lookup_id,)).fetchone()
                                    cs_list = parse_content_sources(cs_row['content_sources'] if cs_row else None)
                                    if not any(s['source'] == new_source for s in cs_list):
                                        cs_list.append({'source': new_source, 'detail': new_detail, 'added_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')})
                                        conn.execute('UPDATE media_items SET content_sources = ? WHERE id = ?',
                                                     (serialize_content_sources(cs_list), lookup_id))
                                except Exception as _cs_err:
                                    logging.warning(f"Failed to write content_sources for collected item {lookup_id}: {_cs_err}")
                                if item.get('source_position') is not None:
                                    conn.execute(
                                        "UPDATE media_items SET source_position=? WHERE id=?",
                                        (item['source_position'], lookup_id)
                                    )
                    skip_stats['already_collected_or_upgrading'] += 1; items_skipped += 1; continue

            if item_type == 'movie':
                skip = False; media_id_vs = imdb_id or tmdb_id
                existing_versions_set_vs = set(); existing_states_set_vs = set()
                if imdb_id and imdb_id in existing_movies:
                    for version_vs, state_vs, _ in existing_movies[imdb_id]:
                        existing_versions_set_vs.add(version_vs); existing_states_set_vs.add(state_vs)
                if tmdb_id and tmdb_id in existing_movies and (not imdb_id or imdb_id != tmdb_id):
                    for version_vs, state_vs, _ in existing_movies[tmdb_id]:
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
                if skip:
                    if _plex_labels_active:
                        new_source = item.get('content_source')
                        new_detail = item.get('content_source_detail')
                        if new_source and new_detail and new_detail.lower() != 'unknown':
                            lookup_id = _get_existing_item_id_any_state(conn, imdb_id, tmdb_id, item_type, item)
                            if lookup_id:
                                pending_source_records.append((lookup_id, new_source, new_detail))
                    items_skipped += 1; continue
            else: # Episode
                season_number_vs = item.get('season_number'); episode_number_vs = item.get('episode_number')
                skip = False; media_id_vs = imdb_id or tmdb_id
                episode_key_vs = (media_id_vs, season_number_vs, episode_number_vs)
                existing_versions_set_vs = set(); existing_states_set_vs = set()
                imdb_key_vs = None; tmdb_key_vs = None
                if imdb_id:
                    imdb_key_vs = (str(imdb_id), season_number_vs, episode_number_vs)
                    if imdb_key_vs in existing_episodes:
                        for version_vs, state_vs, _, _ in existing_episodes[imdb_key_vs]:
                            existing_versions_set_vs.add(version_vs); existing_states_set_vs.add(state_vs)
                if tmdb_id:
                    tmdb_key_vs = (str(tmdb_id), season_number_vs, episode_number_vs)
                    if tmdb_key_vs in existing_episodes and (not imdb_key_vs or imdb_key_vs != tmdb_key_vs):
                        for version_vs, state_vs, _, _ in existing_episodes[tmdb_key_vs]:
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
                if skip:
                    if _plex_labels_active:
                        new_source = item.get('content_source')
                        new_detail = item.get('content_source_detail')
                        if new_source and new_detail and new_detail.lower() != 'unknown':
                            lookup_id = _get_existing_item_id_any_state(conn, imdb_id, tmdb_id, item_type, item)
                            if lookup_id:
                                pending_source_records.append((lookup_id, new_source, new_detail))
                    items_skipped += 1; continue

            filtered_media_items_batch_after_existence_check.append(item)

        media_items_batch = filtered_media_items_batch_after_existence_check

        _tmdb_lookup_cache = {}  # Cache tmdb lookups within this batch to avoid repeated calls for the same imdb_id
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
                imdb_id_lookup = item['imdb_id']
                if imdb_id_lookup in _tmdb_lookup_cache:
                    cached_tmdb = _tmdb_lookup_cache[imdb_id_lookup]
                    if cached_tmdb:
                        item['tmdb_id'] = cached_tmdb
                else:
                    from metadata.metadata import get_tmdb_id_and_media_type
                    tmdb_id_meta, media_type_meta = get_tmdb_id_and_media_type(imdb_id_lookup)
                    if tmdb_id_meta:
                        item['tmdb_id'] = str(tmdb_id_meta)
                        _tmdb_lookup_cache[imdb_id_lookup] = str(tmdb_id_meta)
                    else:
                        _tmdb_lookup_cache[imdb_id_lookup] = None
                        logging.warning(f"Unable to retrieve tmdb_id for {item.get('title', 'Unknown')} (IMDb ID: {imdb_id_lookup})")

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

            # Resolve tags from content source config (Plex mode NZB folder routing)
            # item may already have tags set (e.g. from content requestor), otherwise look up from source config
            _item_tags = item.get('tags') or ''
            if not _item_tags:
                _cs_id = item.get('content_source', '')
                if _cs_id:
                    try:
                        _cs_config = config.get('Content Sources', {}).get(_cs_id, {})
                        _cs_tags = _cs_config.get('tags', [])
                        if isinstance(_cs_tags, list) and _cs_tags:
                            _item_tags = ','.join(t.strip() for t in _cs_tags if t.strip())
                    except Exception:
                        pass

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
                            # Check cache first to avoid redundant API calls
                            cached_entry = imdb_trakt_cache.get(imdb_id)
                            if cached_entry and 'trakt_id' in cached_entry:
                                trakt_id = str(cached_entry['trakt_id'])
                                cache_hits += 1
                                logging.debug(f"Cache hit: IMDB {imdb_id} → Trakt {trakt_id}")
                            else:
                                # Cache miss - perform API lookup
                                trakt_search_results = fetch_items_from_trakt(f"/search/imdb/{imdb_id}")
                                cache_misses += 1
                                trakt_id = None

                                if trakt_search_results and isinstance(trakt_search_results, list) and len(trakt_search_results) > 0:
                                    if 'movie' in trakt_search_results[0] and trakt_search_results[0]['movie'].get('ids', {}).get('trakt'):
                                        trakt_id = str(trakt_search_results[0]['movie']['ids']['trakt'])

                                        # Cache the newly fetched trakt_id
                                        imdb_trakt_cache[imdb_id] = {
                                            'trakt_id': trakt_id,
                                            'cached_at': datetime.now().isoformat()
                                        }
                                        logging.debug(f"Cached: IMDB {imdb_id} → Trakt {trakt_id}")
                                    else:
                                        logging.warning(f"Could not extract Trakt ID from search results for {imdb_id}")
                                else:
                                    logging.info(f"No Trakt search results found for {imdb_id}")

                            # Use the trakt_id (from cache or freshly fetched)
                            if trakt_id:
                                trakt_lists = fetch_items_from_trakt(f"/movies/{trakt_id}/lists/personal/popular")
                                if trakt_lists:
                                    for trakt_list in trakt_lists:
                                        if re.search(r'(latest|new).*?(releases)', trakt_list.get('name', ''), re.IGNORECASE):
                                            early_release_flag = True; break
                                else:
                                    logging.warning(f"Failed to fetch Trakt lists for movie {trakt_id}")
                        except Exception as e:
                            logging.error(f"Error checking Trakt early release for {imdb_id}: {str(e)}")
                            skip_stats['trakt_error'] += 1
                    
                    # Battery year fallback: process_metadata may have gotten year=None if
                    # battery didn't have the item yet. Try battery now before inserting.
                    if not item.get('year') and item.get('imdb_id'):
                        try:
                            from cli_battery.app.direct_api import DirectAPI as _DirectAPI
                            _batt_meta, _ = _DirectAPI.get_movie_metadata(item['imdb_id'])
                            if _batt_meta and _batt_meta.get('year'):
                                item['year'] = _batt_meta['year']
                            elif _batt_meta and _batt_meta.get('release_date'):
                                _rd = str(_batt_meta['release_date'])
                                if len(_rd) >= 4 and _rd[:4].isdigit():
                                    item['year'] = int(_rd[:4])
                        except Exception:
                            pass

                    movie_data = (
                        item.get('imdb_id'), item.get('tmdb_id'), normalized_title, item.get('year'),
                        item.get('release_date'), 'Wanted', 'movie', datetime.now(), version, genres, item.get('runtime'),
                        item.get('country', '').lower(), item.get('content_source'), item.get('content_source_detail'),
                        item.get('physical_release_date'), item.get('theatrical_release_date'), early_release_flag,
                        item.get('source_position'), item.get('selected_folder'), item.get('selected_folder_is_custom', False),
                        _item_tags or None
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

                    # Battery fallback for Unknown release dates
                    release_date = item.get('release_date')
                    if not release_date or str(release_date).lower() == 'unknown':
                        imdb_id = item.get('imdb_id')
                        season_num = item.get('season_number')
                        episode_num = item.get('episode_number')

                        if imdb_id and season_num is not None and episode_num is not None:
                            try:
                                from cli_battery.app.direct_api import DirectAPI
                                metadata, _ = DirectAPI.get_show_metadata(imdb_id)

                                if metadata and 'seasons' in metadata:
                                    season_data = metadata['seasons'].get(str(season_num))
                                    if season_data and 'episodes' in season_data:
                                        episode_data_battery = season_data['episodes'].get(str(episode_num))
                                        if episode_data_battery and 'first_aired' in episode_data_battery:
                                            first_aired = episode_data_battery['first_aired']
                                            if first_aired:
                                                # Extract date from first_aired (format: "2026-02-14 04:00:00" or "2026-02-14T04:00:00")
                                                try:
                                                    first_aired_str = str(first_aired).replace('T', ' ')
                                                    if ' ' in first_aired_str:
                                                        release_date = first_aired_str.split(' ', 1)[0][:10]  # YYYY-MM-DD
                                                    else:
                                                        release_date = first_aired_str[:10]
                                                    logging.info(f"Battery fallback: Found air date {release_date} for {normalized_title} S{season_num}E{episode_num}")
                                                except Exception as e:
                                                    logging.warning(f"Could not parse Battery first_aired '{first_aired}': {e}")
                            except Exception as e:
                                logging.debug(f"Battery fallback failed for {normalized_title} S{season_num}E{episode_num}: {e}")

                    episode_data = (
                        item.get('imdb_id'), item.get('tmdb_id'), normalized_title, item.get('year'),
                        release_date, initial_state, 'episode',
                        item['season_number'], item['episode_number'], item.get('episode_title', ''),
                        datetime.now(), version, item.get('runtime'), airtime, genres, item.get('country', '').lower(),
                        blacklisted_date, item.get('requested_season', False), item.get('content_source'), item.get('content_source_detail'),
                        item.get('source_position'), item.get('selected_folder'), item.get('selected_folder_is_custom', False),
                        _item_tags or None
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
                (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated, version, genres, runtime, country, content_source, content_source_detail, physical_release_date, theatrical_release_date, early_release, source_position, selected_folder, selected_folder_is_custom, tags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', movies_to_insert)

        if episodes_to_insert:
            conn.executemany('''
                INSERT INTO media_items
                (imdb_id, tmdb_id, title, year, release_date, state, type, season_number, episode_number,
                 episode_title, last_updated, version, runtime, airtime, genres, country, blacklisted_date,
                 requested_season, content_source, content_source_detail, source_position, selected_folder, selected_folder_is_custom, tags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', episodes_to_insert)

        # Always commit here — the content_sources/source_position UPDATEs made
        # above (line ~520) while scanning already-Collected items are on this
        # same connection and would otherwise stay open into add_label_to_item
        # below, which opens its OWN connection and self-deadlocks against the
        # uncommitted write lock (each retry then burns ~870s before failing).
        conn.commit()

        # Apply labels for secondary sources on already-Collected items
        if pending_secondary_labels:
            try:
                from utilities.plex_label_manager import (
                    is_plex_labels_enabled_anywhere,
                    determine_labels_for_item,
                    add_label_to_item
                )
                if is_plex_labels_enabled_anywhere():
                    for (existing_id, src, detail) in pending_secondary_labels:
                        temp_item = {'content_source': src, 'content_source_detail': detail}
                        labels = determine_labels_for_item(temp_item)
                        for label in labels:
                            try:
                                add_label_to_item(existing_id, label, src, apply_to_plex=True)
                                logging.info(f"Applied secondary source label '{label}' from {src} to item {existing_id}")
                            except Exception as e:
                                logging.warning(f"Failed to apply secondary source label '{label}' from {src} to item {existing_id}: {e}")
            except Exception as e:
                logging.warning(f"Failed to apply secondary source labels: {e}")

        # Record secondary sources for not-yet-Collected items (for future label application when item reaches Collected)
        # Only runs when plex_labels is enabled for at least one content source
        if pending_source_records:
            try:
                from utilities.plex_label_manager import is_plex_labels_enabled_anywhere, parse_content_sources, serialize_content_sources
                _labels_enabled = is_plex_labels_enabled_anywhere()
            except Exception:
                _labels_enabled = False
        if pending_source_records and _labels_enabled:
            from utilities.plex_label_manager import parse_content_sources, serialize_content_sources
            for (existing_id, src, detail) in pending_source_records:
                try:
                    row = conn.execute('SELECT content_sources FROM media_items WHERE id = ?', (existing_id,)).fetchone()
                    sources = parse_content_sources(row['content_sources'] if row else None)
                    if not any(s['source'] == src for s in sources):
                        sources.append({'source': src, 'detail': detail, 'added_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')})
                        conn.execute('UPDATE media_items SET content_sources = ? WHERE id = ?',
                                     (serialize_content_sources(sources), existing_id))
                        logging.debug(f"Recorded secondary source '{src}' on item {existing_id} for future label application")
                except Exception as e:
                    logging.warning(f"Failed to record secondary source for item {existing_id}: {e}")
            conn.commit()

        # Send notifications for newly added items
        if (movies_to_insert or episodes_to_insert) and items_added > 0:
            try:
                from routes.notifications import send_notifications
                from routes.settings_routes import get_enabled_notifications_for_category
                from routes.extensions import app
                from database.database_reading import get_all_media_items

                with app.app_context():
                    response = get_enabled_notifications_for_category('wanted')
                    if response.json['success']:
                        enabled_notifications = response.json['enabled_notifications']
                        if enabled_notifications:
                            # Get the newly inserted items from the database
                            notifications_to_send = []

                            # Build a set of unique identifiers for the items we just inserted
                            inserted_identifiers = set()
                            for movie_data in movies_to_insert:
                                # movie_data tuple: (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated, version, ...)
                                imdb_id, tmdb_id, title, year = movie_data[0], movie_data[1], movie_data[2], movie_data[3]
                                version = movie_data[8]
                                inserted_identifiers.add((imdb_id, tmdb_id, title, year, version, 'movie'))

                            for episode_data in episodes_to_insert:
                                # episode_data tuple: (imdb_id, tmdb_id, title, year, release_date, state, type, season_number, episode_number, ...)
                                imdb_id, tmdb_id, title, year = episode_data[0], episode_data[1], episode_data[2], episode_data[3]
                                season_num, episode_num = episode_data[7], episode_data[8]
                                version = episode_data[11]
                                inserted_identifiers.add((imdb_id, tmdb_id, title, year, version, 'episode', season_num, episode_num))

                            # Query for items in Wanted state that match our inserted items
                            wanted_items = get_all_media_items(state='Wanted')

                            for db_item in wanted_items:
                                # Check if this item matches one we just inserted
                                item_type = db_item.get('type', 'unknown')
                                if item_type == 'movie':
                                    identifier = (
                                        db_item.get('imdb_id'),
                                        db_item.get('tmdb_id'),
                                        db_item.get('title'),
                                        db_item.get('year'),
                                        db_item.get('version'),
                                        'movie'
                                    )
                                    if identifier in inserted_identifiers and not db_item.get('upgrading'):
                                        notification_data = {
                                            'id': db_item.get('id'),
                                            'title': db_item.get('title', 'Unknown Title'),
                                            'type': item_type,
                                            'year': db_item.get('year', ''),
                                            'version': db_item.get('version', ''),
                                            'season_number': None,
                                            'episode_number': None,
                                            'new_state': 'Wanted',
                                            'is_upgrade': False,
                                            'upgrading_from': None
                                        }
                                        notifications_to_send.append(notification_data)
                                elif item_type == 'episode':
                                    identifier = (
                                        db_item.get('imdb_id'),
                                        db_item.get('tmdb_id'),
                                        db_item.get('title'),
                                        db_item.get('year'),
                                        db_item.get('version'),
                                        'episode',
                                        db_item.get('season_number'),
                                        db_item.get('episode_number')
                                    )
                                    if identifier in inserted_identifiers and not db_item.get('upgrading'):
                                        notification_data = {
                                            'id': db_item.get('id'),
                                            'title': db_item.get('title', 'Unknown Title'),
                                            'type': item_type,
                                            'year': db_item.get('year', ''),
                                            'version': db_item.get('version', ''),
                                            'season_number': str(db_item.get('season_number', '')) if db_item.get('season_number') is not None else None,
                                            'episode_number': str(db_item.get('episode_number', '')) if db_item.get('episode_number') is not None else None,
                                            'new_state': 'Wanted',
                                            'is_upgrade': False,
                                            'upgrading_from': None
                                        }
                                        notifications_to_send.append(notification_data)

                            if notifications_to_send:
                                send_notifications(notifications_to_send, enabled_notifications, notification_category='state_change')
                                logging.info(f"Sent Wanted state notifications for {len(notifications_to_send)} items")
            except Exception as e:
                logging.error(f"Failed to send Wanted state change notifications: {str(e)}")

        # Immediately trigger a Wanted queue run if new items were added
        if items_added > 0:
            try:
                from queues.queue_manager import QueueManager
                QueueManager().process_wanted()
            except Exception as e:
                logging.error(f"Error processing Wanted queue after adding items: {str(e)}", exc_info=True)
        
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
        if skip_stats['already_collected_or_upgrading'] > 0:
             skip_report.append(f"- {skip_stats['already_collected_or_upgrading']} items skipped because they are already Collected or Upgrading")
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
        
        return items_added
    except Exception as e:
        logging.error(f"Error adding wanted items: {str(e)}", exc_info=True)
        conn.rollback()
        raise
    finally:
        # Save cache and log statistics
        save_imdb_trakt_cache(imdb_trakt_cache)
        total_checks = cache_hits + cache_misses
        if total_checks > 0:
            cache_hit_rate = (cache_hits / total_checks) * 100
            logging.info(f"Trakt cache stats: {cache_hits} hits, {cache_misses} misses ({cache_hit_rate:.1f}% hit rate)")

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
                        item.get('content_source_detail'), item.get('physical_release_date'),
                        item.get('source_position')
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
                        item.get('content_source_detail'), item.get('source_position')
                    ))
    
    if movie_items:
        conn.executemany('''
            INSERT INTO media_items
            (imdb_id, tmdb_id, title, year, release_date, state, type, last_updated,
             version, genres, runtime, country, content_source, content_source_detail, physical_release_date,
             source_position)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', movie_items)
        processed['movies'] += len(movie_items)

    if episode_items:
        conn.executemany('''
            INSERT INTO media_items
            (imdb_id, tmdb_id, title, year, release_date, state, type, season_number,
             episode_number, episode_title, last_updated, version, runtime, airtime,
             genres, country, blacklisted_date, requested_season, content_source,
             content_source_detail, source_position)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', episode_items)
        processed['episodes'] += len(episode_items)