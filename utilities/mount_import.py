"""Import files from a debrid/rclone mount into the library as symlinks.

Walks a mount path, matches each media file to a title, adds or adopts the
media_items row and creates the symlink. Runs as a background task; progress
for each task id is kept in rclone_scan_progress for the progress stream.
Used by Debug Functions ("Rclone to symlinks"), the external-add webhook and
the external mount scanner.
"""

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from cli_battery.app.direct_api import DirectAPI
from database.database_writing import add_media_item
from routes.notifications import get_enabled_notifications, send_notifications
from scraper.functions.ptt_parser import parse_with_ptt
from utilities.local_library_scan import create_symlink, get_symlink_path
from utilities.plex_functions import plex_update_item
from utilities.reverse_parser import parse_filename_for_version
from utilities.settings import get_setting, set_setting



# Progress per task id, read by the /debug/api/rclone_scan_progress stream.
rclone_scan_progress = {}


def _describe_add_media_item_failure(item: dict) -> str:
    """Explain why add_media_item returned None.

    It swallows several distinct failures into a bare None — a ghostlist or
    blacklist block, and any sqlite3.Error (IntegrityError included) — so the
    caller cannot tell them apart. 'add_media_item no ID returned' on its own
    sends you digging through add_media_item to guess which one happened.
    Runs only on the failure path, so the extra query costs nothing normally.
    """
    try:
        from database.core import get_db_connection
        imdb_id = item.get('imdb_id')
        tmdb_id = item.get('tmdb_id')
        item_type = item.get('type')
        if not (imdb_id or tmdb_id):
            return ("add_media_item returned None and the item has neither an IMDb "
                    "nor a TMDB ID — check the metadata stage")

        # Must mirror add_media_item's guard exactly, including the
        # version-scoping of the 'Blacklisted' half — an unscoped query here
        # would blame the ghostlist for a failure it did not cause.
        target_version = (item.get('version') or '').rstrip('*')
        guard = ("(ghostlisted = 1 OR (state = 'Blacklisted' "
                 "AND RTRIM(COALESCE(version, ''), '*') = ?))")

        conn = get_db_connection()
        try:
            # Key presence, not value truthiness -- must match add_media_item's
            # own guard exactly ('season_number' in item and 'episode_number' in
            # item), or this can silently take the unscoped branch/match the
            # wrong row and misreport why an add failed.
            if item_type == 'episode' and 'season_number' in item and 'episode_number' in item:
                query = ('SELECT id, state, ghostlisted, version FROM media_items '
                         'WHERE (imdb_id = ? OR tmdb_id = ?) AND type = ? '
                         'AND season_number = ? AND episode_number = ? '
                         f'AND {guard} LIMIT 1')
                params = [imdb_id, tmdb_id, item_type, item.get('season_number'),
                          item.get('episode_number'), target_version]
            else:
                query = ('SELECT id, state, ghostlisted, version FROM media_items '
                         'WHERE (imdb_id = ? OR tmdb_id = ?) AND type = ? '
                         f'AND {guard} LIMIT 1')
                params = [imdb_id, tmdb_id, item_type, target_version]
            row = conn.execute(query, params).fetchone()
        finally:
            conn.close()

        if row:
            reason = 'ghostlisted' if row['ghostlisted'] else f"state={row['state']}"
            return (f"blocked by add_media_item: existing item id={row['id']} is {reason} "
                    f"(version={row['version']}). An external add should pass "
                    f"user_initiated=True to unghost and update it in place.")
        return ("add_media_item returned None but no ghostlisted/blacklisted row "
                "matches — check the preceding 'SQLite error' or 'Unexpected error' "
                "log line from add_media_item for the real cause")
    except Exception as e:
        return f"add_media_item returned None; could not determine why ({e})"


def _run_rclone_to_symlink_task(rclone_mount_path_str, symlink_base_path_str, dry_run, task_id, trigger_plex_update_on_success: bool = False, assumed_item_title_from_path: str = None, user_initiated: bool = False): # Add new parameter
    """Background task to scan Rclone mount, fetch metadata, and create DB entries/symlinks.

    user_initiated is passed straight to add_media_item, where it bypasses the
    ghostlist/blacklist guard. Only set it for an import a person actually asked
    for -- the rclone webhook, which fires because someone put content in the
    debrid account by hand. It must stay False for anything running on a timer:
    the mount also fills up with cli_debrid's own leftovers (cache-check probes,
    torrents orphaned when a grab failed), and bypassing the guard for those
    re-imports the exact releases the queues just blacklisted.
    """
    global rclone_scan_progress

    # --- Progress File Setup ---
    db_content_dir = os.environ.get('USER_DB_CONTENT')
    if not db_content_dir:
        # Fallback if env var is not set (should not happen if main.py runs first)
        logging.error(f"[RcloneScan {task_id}] USER_DB_CONTENT environment variable not found. Progress persistence will be disabled for this run.")
        progress_file_path = None 
    else:
        progress_file_path = os.path.join(db_content_dir, 'rclone_to_symlink_processed_files.json')
        progress_file_path = None # REMOVED

    processed_original_files = set()
    if progress_file_path:
        try:
            if os.path.exists(progress_file_path):
                with open(progress_file_path, 'r') as f:
                    processed_original_files = set(json.load(f))
                logging.info(f"[RcloneScan {task_id}] Loaded {len(processed_original_files)} previously processed file paths.")
        except (json.JSONDecodeError, OSError) as e:
            logging.warning(f"[RcloneScan {task_id}] Could not load progress file {progress_file_path}: {e}. Starting with empty progress.")
            processed_original_files = set()

    def save_rclone_progress():
        if progress_file_path:
            try:
                with open(progress_file_path, 'w') as f:
                    json.dump(list(processed_original_files), f, indent=2)
            except OSError as e:
                logging.error(f"[RcloneScan {task_id}] Could not save progress file {progress_file_path}: {e}")
    # --- End Progress File Setup ---

    MOVIE_SIZE_THRESHOLD_BYTES = 300 * 1024 * 1024 # 300 MB

    rclone_scan_progress[task_id] = {
        'status': 'starting',
        'message': 'Initializing Rclone scan...',
        'total_files_scanned': 0,
        'media_files_found': 0,
        'items_processed': 0,
        'items_added_to_db': 0,
        'symlinks_created': 0,
        'parser_errors': 0,
        'metadata_errors': 0,
        'db_errors': 0,
        'symlink_errors': 0,
        'skipped_duplicates': 0,
        'skipped_due_to_size': 0, # New counter
        'skipped_previously_processed': 0, # New counter
        'preview': [], 
        'errors': [], 
        'complete': False
    }

    # REMOVED: get_largest_video_file_in_folder helper function

    skipped_previously_processed_count = 0 # Local counter
    skipped_due_to_size_count = 0 # Local counter for size skips

    def update_progress(**kwargs):
        if task_id in rclone_scan_progress:
            progress_data = rclone_scan_progress[task_id]
            progress_data.update(kwargs)
            if 'preview' in progress_data and len(progress_data['preview']) > 5:
                 progress_data['preview'] = progress_data['preview'][:5]
        else:
            logging.warning(f"Rclone scan Task ID {task_id} not found in progress dict during update.")

    try:
        rclone_mount_path = Path(rclone_mount_path_str)
        symlink_base_path_setting_backup = get_setting('File Management', 'symlinked_files_path')

        # Check if rclone_mount_path is a directory
        if not rclone_mount_path.is_dir():
            raise ValueError(f"Rclone Mount Path is not a valid directory: {rclone_mount_path_str}")

        if not symlink_base_path_str:
             raise ValueError("Symlink Base Path cannot be empty.")
        
        logging.info(f"[RcloneScan {task_id}] Temporarily setting symlink path to: {symlink_base_path_str}")
        try:
             set_setting('File Management', 'symlinked_files_path', symlink_base_path_str)
        except Exception as set_setting_err:
             raise RuntimeError(f"Failed to temporarily set symlink base path: {set_setting_err}")

        update_progress(status='scanning', message='Scanning Rclone mount path...')
        video_extensions = {'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm', '.mpeg', '.mpg'}
        total_files_scanned = 0
        media_files_found = 0
        items_processed = 0
        items_added_to_db = 0
        symlinks_created = 0
        parser_errors = 0
        metadata_errors = 0
        db_errors = 0
        symlink_errors = 0
        skipped_duplicates = 0
        # REMOVED: skipped_smaller_movies_in_folder_count = 0
        preview_list = []
        error_list = []
        direct_api = DirectAPI()

        for item_path in rclone_mount_path.rglob('*'):
            total_files_scanned += 1
            if total_files_scanned % 100 == 0:
                 update_progress(total_files_scanned=total_files_scanned, message=f'Scanned {total_files_scanned} files...')

            if not (item_path.is_file() and item_path.suffix.lower() in video_extensions):
                continue
            
            original_file_path_str = str(item_path) # Used for progress tracking

            # Check if this specific file was already processed and recorded
            if original_file_path_str in processed_original_files:
                logging.info(f"[RcloneScan {task_id}] Skipping previously processed file: {item_path.name}")
                skipped_previously_processed_count += 1
                update_progress(skipped_previously_processed=skipped_previously_processed_count)
                continue

            # REMOVED: Logic for files_to_skip_in_movie_folders
            
            media_files_found += 1
            logging.debug(f"[RcloneScan {task_id}] Evaluating media file: {original_file_path_str}")

            # --- Start of merged parsing logic ---
            parsed_info_folder = {}
            parsed_version_folder = None
            folder_name = item_path.parent.name
            if folder_name:
                try:
                    parsed_info_folder = parse_with_ptt(folder_name)
                    if parsed_info_folder.get('parsing_error'): parsed_info_folder = {}
                    parsed_version_folder = parse_filename_for_version(folder_name)
                except Exception: parsed_info_folder, parsed_version_folder = {}, None
            
            parsed_info_file, parsed_version_file = {}, None
            try:
                parsed_info_file = parse_with_ptt(item_path.name)
                if parsed_info_file.get('parsing_error'): logging.warning(f"[RcloneScan {task_id}] PTT filename parse error for {item_path.name}")
                parsed_version_file = parse_filename_for_version(item_path.name)
            except Exception as e:
                logging.error(f"[RcloneScan {task_id}] PTT/Reverse filename parse failed for {item_path.name}: {e}. Skipping.")
                parser_errors += 1; update_progress(parser_errors=parser_errors); continue

            def get_prioritized_value(key, from_folder, from_file, default=None):
                folder_val = from_folder.get(key)
                file_val = from_file.get(key)
                is_folder_val_empty = folder_val is None or (isinstance(folder_val, str) and not folder_val.strip())
                is_file_val_empty = file_val is None or (isinstance(file_val, str) and not file_val.strip())
                if not is_folder_val_empty: return folder_val
                if not is_file_val_empty: return file_val
                return default

            parsed_title = get_prioritized_value('title', parsed_info_folder, parsed_info_file)
            parsed_year = get_prioritized_value('year', parsed_info_folder, parsed_info_file)
            parsed_season_folder_val, parsed_season_file_val = parsed_info_folder.get('season'), parsed_info_file.get('season')
            parsed_episode_folder_val, parsed_episode_file_val = parsed_info_folder.get('episode'), parsed_info_file.get('episode')
            parsed_season = parsed_season_folder_val if parsed_season_folder_val is not None else parsed_season_file_val
            parsed_episode = parsed_episode_folder_val if parsed_episode_folder_val is not None else parsed_episode_file_val
            if isinstance(parsed_season, list) and parsed_season: parsed_season = parsed_season[0]
            
            # Handle multi-episode files properly
            if isinstance(parsed_episode, list) and len(parsed_episode) > 1:
                # For multi-episode files, create a range format like "E17-E18"
                parsed_episode = f"E{parsed_episode[0]}-E{parsed_episode[-1]}"
            elif isinstance(parsed_episode, list) and parsed_episode:
                parsed_episode = parsed_episode[0]

            is_version_folder_empty = parsed_version_folder is None or not str(parsed_version_folder).strip()
            is_version_file_empty = parsed_version_file is None or not str(parsed_version_file).strip()
            current_parsed_version = 'Default'
            if not is_version_folder_empty: current_parsed_version = str(parsed_version_folder)
            elif not is_version_file_empty: current_parsed_version = str(parsed_version_file)
            current_parsed_version = current_parsed_version.strip('*') if current_parsed_version else 'Default'
            if not current_parsed_version: current_parsed_version = 'Default'

            current_parsed_type = 'episode' if parsed_season is not None or parsed_episode is not None else 'movie'
            if not parsed_title:
                logging.warning(f"[RcloneScan {task_id}] No title for {item_path.name}. Skipping.")
                parser_errors += 1; update_progress(parser_errors=parser_errors); continue
            # --- End of merged parsing logic ---

            # --- Movie: Size check logic ---
            if current_parsed_type == 'movie':
                try:
                    file_size = item_path.stat().st_size
                    if file_size < MOVIE_SIZE_THRESHOLD_BYTES:
                        logging.info(f"[RcloneScan {task_id}] Skipping movie '{item_path.name}' due to size ({file_size / (1024*1024):.2f}MB) below threshold ({MOVIE_SIZE_THRESHOLD_BYTES / (1024*1024):.2f}MB).")
                        skipped_due_to_size_count += 1
                        update_progress(skipped_due_to_size=skipped_due_to_size_count)
                        continue 
                except OSError as e:
                    logging.warning(f"[RcloneScan {task_id}] Could not get stats for movie file {item_path.name}: {e}. Skipping.")
                    # parser_errors += 1 # Or a new counter for stat errors
                    # update_progress(parser_errors=parser_errors)
                    continue
            # --- End Movie Size Check Logic ---
            
            update_progress(message=f'Processing: {item_path.name} ({current_parsed_type})')
            items_processed += 1

            # 2. Fetch Metadata
            metadata = None
            final_imdb_id, final_tmdb_id = None, None
            try:
                item_id_to_use = None
                search_type_for_api = 'show' if current_parsed_type == 'episode' else 'movie'
                
                # Determine titles from filename and folder
                filename_title_raw = parsed_info_file.get('title')
                cleaned_filename_title = filename_title_raw.replace('.', ' ') if filename_title_raw and filename_title_raw.strip() else None

                folder_title_raw = parsed_info_folder.get('title')
                cleaned_folder_title = folder_title_raw.replace('.', ' ') if folder_title_raw and folder_title_raw.strip() else None

                final_search_results = None
                title_that_yielded_search_results = None

                # For episodes, prioritize folder title (show name) over filename title (episode title)
                # For movies, prioritize filename title over folder title
                if current_parsed_type == 'episode' and cleaned_folder_title:
                    # Attempt 1: Search using folder title (show name) for episodes
                    logging.info(f"[RcloneScan {task_id}] Attempting primary search with folder title (show name): '{cleaned_folder_title}', Year='{parsed_year}', Type='{search_type_for_api}' for File='{item_path.name}'")
                    search_results_folder, _ = direct_api.search_media(query=cleaned_folder_title, year=parsed_year, media_type=search_type_for_api)
                    if search_results_folder:
                        final_search_results = search_results_folder
                        title_that_yielded_search_results = cleaned_folder_title
                        logging.info(f"[RcloneScan {task_id}] Primary search with folder title '{cleaned_folder_title}' was successful.")
                    else:
                        logging.warning(f"[RcloneScan {task_id}] Primary search with folder title '{cleaned_folder_title}' yielded no results.")
                        
                    # Attempt 2: If folder title search failed, try filename title as fallback
                    if not final_search_results and cleaned_filename_title and cleaned_filename_title != cleaned_folder_title:
                        logging.info(f"[RcloneScan {task_id}] Attempting fallback search with filename title: '{cleaned_filename_title}', Year='{parsed_year}', Type='{search_type_for_api}' for File='{item_path.name}'")
                        search_results_file, _ = direct_api.search_media(query=cleaned_filename_title, year=parsed_year, media_type=search_type_for_api)
                        if search_results_file:
                            final_search_results = search_results_file
                            title_that_yielded_search_results = cleaned_filename_title
                            logging.info(f"[RcloneScan {task_id}] Fallback search with filename title '{cleaned_filename_title}' was successful.")
                        else:
                            logging.warning(f"[RcloneScan {task_id}] Fallback search with filename title '{cleaned_filename_title}' also yielded no results.")
                    elif not final_search_results and cleaned_filename_title and cleaned_filename_title == cleaned_folder_title:
                        logging.debug(f"[RcloneScan {task_id}] Filename title is same as folder title, already attempted or folder title was null; no separate filename title search needed.")
                else:
                    # For movies, use original logic: filename title first, then folder title
                    # Attempt 1: Search using cleaned filename title
                    if cleaned_filename_title:
                        logging.info(f"[RcloneScan {task_id}] Attempting primary search with filename title: '{cleaned_filename_title}', Year='{parsed_year}', Type='{search_type_for_api}' for File='{item_path.name}'")
                        search_results_file, _ = direct_api.search_media(query=cleaned_filename_title, year=parsed_year, media_type=search_type_for_api)
                        if search_results_file:
                            final_search_results = search_results_file
                            title_that_yielded_search_results = cleaned_filename_title
                            logging.info(f"[RcloneScan {task_id}] Primary search with filename title '{cleaned_filename_title}' was successful.")
                        else:
                            logging.warning(f"[RcloneScan {task_id}] Primary search with filename title '{cleaned_filename_title}' yielded no results.")
                    else:
                        logging.debug(f"[RcloneScan {task_id}] No valid cleaned filename title to attempt primary search.")

                    # Attempt 2: If primary search failed or wasn't possible, try folder title (if different and valid)
                    if not final_search_results and cleaned_folder_title and cleaned_folder_title != cleaned_filename_title:
                        logging.info(f"[RcloneScan {task_id}] Attempting fallback search with folder title: '{cleaned_folder_title}', Year='{parsed_year}', Type='{search_type_for_api}' for File='{item_path.name}'")
                        search_results_folder, _ = direct_api.search_media(query=cleaned_folder_title, year=parsed_year, media_type=search_type_for_api)
                        if search_results_folder:
                            final_search_results = search_results_folder
                            title_that_yielded_search_results = cleaned_folder_title
                            logging.info(f"[RcloneScan {task_id}] Fallback search with folder title '{cleaned_folder_title}' was successful.")
                        else:
                            logging.warning(f"[RcloneScan {task_id}] Fallback search with folder title '{cleaned_folder_title}' also yielded no results.")
                    elif not final_search_results and cleaned_folder_title and cleaned_folder_title == cleaned_filename_title:
                        logging.debug(f"[RcloneScan {task_id}] Folder title is same as filename title, already attempted or filename title was null; no separate folder title search needed.")
                
                if not final_search_results:
                    logging.warning(f"[RcloneScan {task_id}] All search attempts failed for file '{item_path.name}'. Parsed folder title: '{cleaned_folder_title}', Parsed filename title: '{cleaned_filename_title}'.")

                # Determine the best title to use for find_best_match_from_results scoring (prioritize filename)
                if cleaned_filename_title:
                    title_for_best_match_selection = cleaned_filename_title
                    logging.debug(f"[RcloneScan {task_id}] Using cleaned filename title for best_match selection: '{title_for_best_match_selection}'")
                elif cleaned_folder_title: # Fallback to folder title if filename title was not usable
                    title_for_best_match_selection = cleaned_folder_title
                    logging.debug(f"[RcloneScan {task_id}] Filename title not suitable, falling back to folder title for best_match selection: '{title_for_best_match_selection}'")
                else: # Should ideally not happen if PTT parsed something for `parsed_title`
                    title_for_best_match_selection = parsed_title # Raw PTT output as last resort
                    logging.debug(f"[RcloneScan {task_id}] No suitable cleaned filename or folder title, falling back to raw parsed_title for best_match selection: '{title_for_best_match_selection}'")
                
                # A Fix Match correction for this title wins outright. The fuzzy
                # search below is stateless, so whatever it mis-resolved once it
                # mis-resolves again for every new release of the same show --
                # undoing the correction each time a file lands.
                from database.match_overrides import find_match_override
                override_imdb_id = find_match_override(
                    [cleaned_folder_title, cleaned_filename_title, parsed_title],
                    parsed_year,
                    'show' if current_parsed_type == 'episode' else 'movie',
                )

                best_match_from_search = None
                if final_search_results and not override_imdb_id:
                    best_match_from_search = DirectAPI.find_best_match_from_results(
                        original_query_title=title_for_best_match_selection, 
                        query_year=parsed_year,
                        search_results=final_search_results
                    )

                if override_imdb_id:
                    logging.info(f"[RcloneScan {task_id}] Using Fix Match override for "
                                 f"'{title_for_best_match_selection}' ({parsed_year}): {override_imdb_id}")
                    item_id_to_use = override_imdb_id
                elif best_match_from_search:
                    logging.info(f"[RcloneScan {task_id}] Best match selected by find_best_match_from_results: {best_match_from_search.get('title')} ({best_match_from_search.get('year')}) using matching title '{title_for_best_match_selection}' (search performed with '{title_that_yielded_search_results}')")
                    item_id_to_use = best_match_from_search.get('imdb_id') or best_match_from_search.get('tmdb_id')
                elif final_search_results: 
                    logging.warning(f"[RcloneScan {task_id}] No confident match from find_best_match_from_results using matching title '{title_for_best_match_selection}'. Falling back to first search result (search performed with '{title_that_yielded_search_results}').")
                    first_match_fallback = final_search_results[0]
                    item_id_to_use = first_match_fallback.get('imdb_id') or first_match_fallback.get('tmdb_id')
                else: 
                    logging.warning(f"[RcloneScan {task_id}] No search results found for file '{item_path.name}' after all attempts to feed into find_best_match_from_results.")
                    item_id_to_use = None

                if item_id_to_use:
                    is_imdb = str(item_id_to_use).startswith('tt')
                    if current_parsed_type == 'movie':
                        imdb_to_fetch_with = None
                        tmdb_known_from_search = None
                        if is_imdb:
                            imdb_to_fetch_with = item_id_to_use
                        else: 
                            tmdb_known_from_search = item_id_to_use # item_id_to_use is TMDB ID string
                            converted_imdb, _ = direct_api.tmdb_to_imdb(tmdb_known_from_search, 'movie')
                            if converted_imdb and str(converted_imdb).strip():
                                imdb_to_fetch_with = str(converted_imdb).strip()
                        
                        if imdb_to_fetch_with:
                            metadata_result, _ = direct_api.get_movie_metadata(imdb_id=imdb_to_fetch_with)
                            if isinstance(metadata_result, dict): # Log keys if it's a dict
                                logging.info(f"[RcloneScan {task_id}] Movie metadata_result keys for IMDb {imdb_to_fetch_with}: {list(metadata_result.keys())}")
                            if metadata_result and isinstance(metadata_result, dict):
                                metadata = metadata_result
                                final_imdb_id = str(metadata.get('imdb_id')).strip() if metadata.get('imdb_id') and str(metadata.get('imdb_id')).strip() else imdb_to_fetch_with
                                # Corrected TMDB ID extraction for movies
                                final_tmdb_id = str(metadata.get('ids', {}).get('tmdb')).strip() if metadata.get('ids', {}).get('tmdb') else tmdb_known_from_search
                            else: 
                                logging.warning(f"[RcloneScan {task_id}] get_movie_metadata for {imdb_to_fetch_with} returned invalid. Using known IDs.")
                                final_imdb_id = imdb_to_fetch_with
                                final_tmdb_id = tmdb_known_from_search
                                metadata = None 
                        elif tmdb_known_from_search: 
                            logging.warning(f"[RcloneScan {task_id}] Only TMDB ID {tmdb_known_from_search} for movie '{parsed_title}'. No IMDb fetch.")
                            final_tmdb_id = tmdb_known_from_search
                            metadata = {'title': parsed_title, 'year': parsed_year, 'id': final_tmdb_id}
                        # If neither imdb_to_fetch_with nor tmdb_known_from_search, IDs remain None.

                    elif current_parsed_type == 'episode':
                        s_num_int, e_num_int = None, None
                        try:
                            if parsed_season is not None: s_num_int = int(parsed_season)
                        except (ValueError, TypeError):
                            logging.warning(f"[RcloneScan {task_id}] Could not convert parsed_season '{parsed_season}' to int for {parsed_title}")
                        try:
                            if parsed_episode is not None: e_num_int = int(parsed_episode)
                        except (ValueError, TypeError):
                            logging.warning(f"[RcloneScan {task_id}] Could not convert parsed_episode '{parsed_episode}' to int for {parsed_title} S{s_num_int}")

                        if s_num_int is None or e_num_int is None:
                            logging.warning(f"[RcloneScan {task_id}] Missing or invalid S ({s_num_int}) or E ({e_num_int}) number for episode-type '{parsed_title}'. Cannot fetch specific episode metadata.")
                            # Keep existing final_imdb_id/final_tmdb_id if they were show IDs from search, but episode metadata is None
                            # This will be caught by 'if not metadata:' later.
                            # To ensure final_imdb_id and final_tmdb_id are set if show search was successful:
                            if is_imdb: final_imdb_id = item_id_to_use
                            else: final_tmdb_id = item_id_to_use # if show search gave TMDB
                        else:
                            # Both s_num_int and e_num_int are valid integers here
                            show_imdb_to_fetch_with = None
                            show_tmdb_known_from_search = None
                            if is_imdb: 
                                show_imdb_to_fetch_with = item_id_to_use
                            else: 
                                show_tmdb_known_from_search = item_id_to_use
                                converted_imdb, _ = direct_api.tmdb_to_imdb(show_tmdb_known_from_search, 'show')
                                if converted_imdb and str(converted_imdb).strip():
                                    show_imdb_to_fetch_with = str(converted_imdb).strip()
                                    
                            if show_imdb_to_fetch_with:
                                show_meta_full, _ = direct_api.get_show_metadata(imdb_id=show_imdb_to_fetch_with)
                                if isinstance(show_meta_full, dict): # Log keys if it's a dict
                                    logging.info(f"[RcloneScan {task_id}] Show show_meta_full keys for IMDb {show_imdb_to_fetch_with}: {list(show_meta_full.keys())}")
                                if show_meta_full and isinstance(show_meta_full, dict):
                                    # Set show's final IDs
                                    final_imdb_id = str(show_meta_full.get('imdb_id')).strip() if show_meta_full.get('imdb_id') and str(show_meta_full.get('imdb_id')).strip() else show_imdb_to_fetch_with
                                    # Correctly get the show's TMDB ID from 'ids.tmdb'
                                    final_tmdb_id = str(show_meta_full.get('ids', {}).get('tmdb')).strip() if show_meta_full.get('ids', {}).get('tmdb') else show_tmdb_known_from_search
                                    # REMOVED: The following line was overwriting final_tmdb_id, likely with a Trakt ID or an incorrect fallback.
                                    # final_tmdb_id = str(show_meta_full.get('id')).strip() if show_meta_full.get('id') and str(show_meta_full.get('id')).strip() else show_tmdb_known_from_search
                                    
                                    season_data_dict = show_meta_full.get('seasons', {})
                                    season_data = season_data_dict.get(str(s_num_int)) # API uses string keys for seasons
                                    if season_data is None: # Fallback for int key, though less likely
                                        season_data = season_data_dict.get(s_num_int)

                                    episode_data = None
                                    if season_data:
                                        episode_data_dict = season_data.get('episodes', {})
                                        episode_data = episode_data_dict.get(str(e_num_int)) # API uses string keys for episodes
                                        if episode_data is None: # Fallback for int key
                                            episode_data = episode_data_dict.get(e_num_int)
                                            
                                    if episode_data:
                                        episode_specific_tmdb_val = episode_data.get('id') 
                                        if not episode_specific_tmdb_val and isinstance(episode_data.get('ids'), dict):
                                            episode_specific_tmdb_val = episode_data.get('ids', {}).get('tmdb')
                                        episode_specific_tmdb_id_str = str(episode_specific_tmdb_val).strip() if episode_specific_tmdb_val and str(episode_specific_tmdb_val).strip() else None

                                        raw_first_aired = episode_data.get('first_aired')
                                        formatted_first_aired = None
                                        if raw_first_aired and isinstance(raw_first_aired, str):
                                            formatted_first_aired = raw_first_aired.split('T')[0]
                                        elif raw_first_aired:
                                            formatted_first_aired = str(raw_first_aired)

                                        metadata = {
                                            'title': show_meta_full.get('title'), 'year': show_meta_full.get('year'), 
                                            'imdb_id': final_imdb_id, 
                                            'tmdb_id': final_tmdb_id, # This will now correctly use the show's TMDB ID
                                            'season_number': s_num_int, 'episode_number': e_num_int,
                                            'episode_title': episode_data.get('title'), 
                                            'air_date': formatted_first_aired, # Use formatted date
                                            'release_date': formatted_first_aired, # Use formatted date
                                            'genres': show_meta_full.get('genres', [])
                                        }
                                    else: 
                                        logging.warning(f"[RcloneScan {task_id}] Episode S{s_num_int}E{e_num_int} not in show data for {final_imdb_id if final_imdb_id else show_imdb_to_fetch_with}")
                                else: 
                                    logging.warning(f"[RcloneScan {task_id}] get_show_metadata for {show_imdb_to_fetch_with} invalid. Using known show IDs.")
                                    final_imdb_id = show_imdb_to_fetch_with
                                    final_tmdb_id = show_tmdb_known_from_search
                            elif show_tmdb_known_from_search: 
                                logging.warning(f"[RcloneScan {task_id}] Only Show TMDB ID {show_tmdb_known_from_search} for '{parsed_title}'.")
                                final_tmdb_id = show_tmdb_known_from_search
                
                if not metadata: 
                    # If metadata is still None, but we have at least one ID (show or movie), log it.
                    # The previous ValueError for "Both IMDb and TMDB IDs missing" is only if *both* are missing *after* this whole block.
                    if final_imdb_id or final_tmdb_id:
                         logging.warning(f"[RcloneScan {task_id}] Full metadata object not constructed for '{parsed_title}', but found IDs: IMDb={final_imdb_id}, TMDB={final_tmdb_id} (File: {item_path.name})")
                    else: # This case should now be rarer with fallback ID assignments
                         raise ValueError(f"Metadata fetch failed AND no usable search/conversion ID ultimately found for '{parsed_title}' (File: {item_path.name})")

                # This final check remains important
                if not final_imdb_id and not final_tmdb_id: 
                    raise ValueError(f"Both IMDb and TMDB IDs are missing post-metadata processing for '{parsed_title}' (File: {item_path.name})")

            except Exception as e: 
                logging.warning(f"[RcloneScan {task_id}] Metadata processing stage for {item_path.name} failed: {e}", exc_info=True)
                metadata_errors += 1; update_progress(metadata_errors=metadata_errors); continue
            
            # 3. Prepare DB Item (original_file_path_str for original_path_for_symlink)
            current_time = datetime.now() # Changed from now_iso
            
            db_title = metadata.get('title') if metadata else parsed_title
            db_year = metadata.get('year') if metadata else parsed_year
            if current_parsed_type == 'episode' and metadata and metadata.get('title'):
                db_title = metadata.get('title') 
            elif not db_title: 
                 db_title = parsed_title

            if current_parsed_type == 'episode' and metadata and metadata.get('year'):
                db_year = metadata.get('year') 
            elif not db_year: 
                db_year = parsed_year

            # Determine release_date based on type and available keys
            final_release_date = None
            if metadata:
                if current_parsed_type == 'movie':
                    # For movies, prioritize 'release_date', then 'released'
                    raw_movie_release_date = metadata.get('release_date') or metadata.get('released')
                    if isinstance(raw_movie_release_date, str):
                        final_release_date = raw_movie_release_date.split('T')[0]
                    elif raw_movie_release_date: # If it exists but not string, log and set to None
                        logging.warning(f"[RcloneScan {task_id}] Movie release date key ('release_date' or 'released') was not a string: {raw_movie_release_date} for {item_path.name}")
                elif current_parsed_type == 'episode':
                    # For episodes, 'release_date' should already be formatted (from 'first_aired')
                    # 'air_date' can be a fallback if 'release_date' wasn't populated during episode metadata construction
                    raw_episode_release_date = metadata.get('release_date') or metadata.get('air_date')
                    if isinstance(raw_episode_release_date, str): # Should already be YYYY-MM-DD
                        final_release_date = raw_episode_release_date
                    elif raw_episode_release_date:
                         logging.warning(f"[RcloneScan {task_id}] Episode release date key ('release_date' or 'air_date') was not a string: {raw_episode_release_date} for {item_path.name}")

            item_content_source = 'external_webhook' if trigger_plex_update_on_success else 'scanned_item' # Determine content_source

            # Determine the value for filled_by_title
            filled_by_title_value = None
            if assumed_item_title_from_path:
                filled_by_title_value = assumed_item_title_from_path
                logging.debug(f"[RcloneScan {task_id}] Using assumed_item_title_from_path for filled_by_title: '{filled_by_title_value}' for item '{item_path.name}'")
            elif item_path and item_path.parent:
                filled_by_title_value = item_path.parent.name
                logging.debug(f"[RcloneScan {task_id}] Using parent folder name for filled_by_title: '{filled_by_title_value}' for item '{item_path.name}'")
            else:
                logging.warning(f"[RcloneScan {task_id}] Could not determine filled_by_title for item '{item_path.name if item_path else 'Unknown Item'}'. It will be None.")

            raw_genres_list = metadata.get('genres', []) if metadata else [] # Get genres as a list

            item_for_db = {
                'imdb_id': final_imdb_id, 
                'tmdb_id': final_tmdb_id,
                'title': db_title,
                'year': db_year,
                'release_date': final_release_date,
                'state': 'Collected', 
                'type': current_parsed_type,
                'season_number': metadata.get('season_number') if metadata else (s_num_int if current_parsed_type == 'episode' else None),
                'episode_number': metadata.get('episode_number') if metadata else (e_num_int if current_parsed_type == 'episode' else None),
                'episode_title': metadata.get('episode_title') if metadata else None, 
                'collected_at': current_time, # Use datetime object
                'original_collected_at': current_time, # Use datetime object
                'original_path_for_symlink': original_file_path_str,
                'version': current_parsed_version, 
                'filled_by_file': item_path.name,
                'filled_by_title': filled_by_title_value, # <<< USE THE DERIVED VALUE HERE
                'metadata_updated': current_time, # Use datetime object
                'genres': raw_genres_list, # Store raw list for get_symlink_path
                'content_source': item_content_source, # Use the determined content_source
            }
            item_for_db_filtered_for_symlink = {k: v for k, v in item_for_db.items() if v is not None} # Use this for get_symlink_path

            # 4. Generate Symlink Path
            try:
                # item_for_db_filtered_for_symlink has 'genres' as a list, which is correct for get_symlink_path
                symlink_dest_path = get_symlink_path(item_for_db_filtered_for_symlink, item_path.name, skip_jikan_lookup=True)
                if not symlink_dest_path: raise ValueError("get_symlink_path returned None")
                
                # Prepare item_for_db_filtered_for_db with genres as JSON string
                item_for_db_filtered_for_db = item_for_db_filtered_for_symlink.copy() # Start with a copy
                if 'genres' in item_for_db_filtered_for_db and isinstance(item_for_db_filtered_for_db['genres'], list):
                    item_for_db_filtered_for_db['genres'] = json.dumps(item_for_db_filtered_for_db['genres'])
                
                item_for_db_filtered_for_db['location_on_disk'] = symlink_dest_path # Add location_on_disk now

            except Exception as e:
                logging.warning(f"[RcloneScan {task_id}] Symlink path gen failed for {item_path.name}: {e}")
                symlink_errors += 1; update_progress(symlink_errors=symlink_errors); continue
            
            # 5. Dry Run or Execution
            if dry_run:
                preview_data = {
                    'original_file': original_file_path_str, 'parsed_title': parsed_title, 
                    'parsed_type': current_parsed_type, 'fetched_title': item_for_db_filtered_for_symlink.get('title'), # use _for_symlink version for title consistency in preview
                    'imdb_id': final_imdb_id, 'tmdb_id': final_tmdb_id,
                    'version': current_parsed_version, 'symlink_path': symlink_dest_path,
                    'action': 'CREATE DB Entry & Symlink'
                }
                preview_list.append(preview_data)
                update_progress(preview=preview_list, items_processed=items_processed)
            else:
                item_id_from_db = None
                try:
                    # add_media_item is called with item_for_db_filtered_for_db,
                    # which has 'genres' as a JSON string
                    #
                    # user_initiated bypasses add_media_item's ghostlist/blacklist
                    # guard, so an existing entry of the same version is unghosted
                    # and updated in place instead of blocked. Only the rclone
                    # webhook sets it: content that reached the mount because a
                    # person put it there is disproportionately likely to be
                    # ghostlisted already, since that is usually why they went and
                    # got it elsewhere. It is a decision for the caller to make --
                    # it was briefly derived from trigger_plex_update_on_success,
                    # which silently handed the bypass to the periodic mount scan
                    # and let it re-import releases the queues had just blacklisted.
                    item_id_from_db = add_media_item(
                        item_for_db_filtered_for_db,
                        user_initiated=user_initiated
                    )
                    if not item_id_from_db:
                        raise Exception(_describe_add_media_item_failure(item_for_db_filtered_for_db))
                    items_added_to_db += 1; update_progress(items_added_to_db=items_added_to_db)
                except sqlite3.IntegrityError:
                    # NOTE: unreachable. add_media_item catches sqlite3.Error —
                    # IntegrityError is a subclass — and returns None instead of
                    # propagating, so a duplicate surfaces through the None branch
                    # above, not here. media_items also declares no UNIQUE/NOT NULL
                    # constraints, so nothing raises IntegrityError in the first
                    # place. Kept only so behaviour does not change if either of
                    # those two facts is ever revisited.
                    logging.warning(f"[RcloneScan {task_id}] DB IntegrityError for {item_path.name}, V:{current_parsed_version}. Likely duplicate.")
                    skipped_duplicates += 1; update_progress(skipped_duplicates=skipped_duplicates)
                    # If it's a duplicate, we should still mark original_file_path_str as processed if we intend to skip it next time
                    # However, if symlink creation was the goal, and DB entry exists, maybe try to symlink?
                    # For now, if DB entry is duplicate, then this file is not "successfully processed" into a *new* DB entry.
                    # This needs careful thought if we want to "adopt" existing DB entries.
                    # Current logic: if DB duplicate, then this file is not "successfully processed" into a *new* DB entry.
                    # This needs careful thought if we want to "adopt" existing DB entries.
                    continue 
                except Exception as e:
                    logging.error(f"[RcloneScan {task_id}] DB add error for {item_path.name}: {e}", exc_info=True)
                    db_errors += 1; error_list.append(f"DB Add Error ({item_path.name}): {e}"); update_progress(db_errors=db_errors, errors=error_list)
                    continue

                if item_id_from_db:
                    try:
                        # Determine if symlink verification should be skipped based on the context
                        # Webhook (trigger_plex_update_on_success=True) -> skip_verification=False (i.e., DO verify)
                        # Debug tool (trigger_plex_update_on_success=False) -> skip_verification=True (i.e., DO NOT verify)
                        skip_verification_for_symlink = not trigger_plex_update_on_success
                        
                        symlink_success = create_symlink(
                            original_file_path_str, 
                            symlink_dest_path, 
                            media_item_id=item_id_from_db, 
                            skip_verification=skip_verification_for_symlink # Dynamically set based on context
                        )
                        if not symlink_success: raise Exception("create_symlink returned False")
                        symlinks_created += 1; update_progress(symlinks_created=symlinks_created)

                        # Sync the file pointers onto the row. Required for the
                        # adopt path: when add_media_item unghosts an existing
                        # entry of the same version it returns that entry's id
                        # after updating only ghostlisted/state/magnet/torrent
                        # fields, so location_on_disk and friends would still
                        # reference the file this import just replaced. A no-op
                        # for a fresh insert, which already has these values.
                        try:
                            from database.database_writing import update_media_item as _update_media_item
                            _location_fields = {
                                k: item_for_db_filtered_for_db.get(k)
                                for k in ('location_on_disk', 'original_path_for_symlink',
                                          'filled_by_file', 'filled_by_title', 'state',
                                          'collected_at', 'version')
                                if item_for_db_filtered_for_db.get(k) is not None
                            }
                            if _location_fields:
                                _update_media_item(item_id_from_db, **_location_fields)
                        except Exception as _loc_err:
                            logging.error(
                                f"[RcloneScan {task_id}] Symlink created but failed to sync file "
                                f"pointers on item {item_id_from_db}: {_loc_err}", exc_info=True)
                        
                        # Successfully processed, add to persistent progress and save
                        processed_original_files.add(original_file_path_str)
                        save_rclone_progress()
                        
                        # --- Conditionally Add Plex Update Call ---
                        if trigger_plex_update_on_success: # Check the new parameter
                            plex_url = get_setting('File Management', 'plex_url_for_symlink', '')
                            plex_token = get_setting('File Management', 'plex_token_for_symlink', '')
                            
                            if plex_url and plex_token:
                                logging.info(f"[RcloneScan {task_id}] Plex configured and update triggered. Attempting library update for: {symlink_dest_path}")
                                try:
                                    # Make sure item_for_db_filtered_for_db has the necessary info (title, year, type etc.)
                                    plex_update_item(item=item_for_db_filtered_for_db)
                                    logging.info(f"[RcloneScan {task_id}] Plex library update triggered for: {symlink_dest_path}")
                                except Exception as plex_err:
                                    logging.error(f"[RcloneScan {task_id}] Failed to trigger Plex update for {symlink_dest_path}: {plex_err}", exc_info=True)
                            else:
                                 logging.debug(f"[RcloneScan {task_id}] Plex URL/Token not configured in 'File Management' settings. Skipping Plex update despite trigger.")
                        else:
                            logging.debug(f"[RcloneScan {task_id}] Plex update not triggered for this task run.")
                        # --- End Plex Update Call ---
                        
                        # --- Send Notification if from external_webhook ---
                        if item_for_db_filtered_for_db.get('content_source') == 'external_webhook':
                            try:
                                notification_item = {
                                    'type': item_for_db_filtered_for_db.get('type'), # 'movie' or 'episode'
                                    'title': item_for_db_filtered_for_db.get('title'),
                                    'year': item_for_db_filtered_for_db.get('year'),
                                    'tmdb_id': str(item_for_db_filtered_for_db.get('tmdb_id')) if item_for_db_filtered_for_db.get('tmdb_id') else None,
                                    'imdb_id': item_for_db_filtered_for_db.get('imdb_id'),
                                    'original_collected_at': item_for_db_filtered_for_db.get('collected_at').isoformat() if item_for_db_filtered_for_db.get('collected_at') else datetime.now().isoformat(),
                                    'version': item_for_db_filtered_for_db.get('version'),
                                    'is_upgrade': False, # New items from rclone webhook are not considered upgrades here
                                    'media_type': 'tv' if item_for_db_filtered_for_db.get('type') == 'episode' else 'movie',
                                    'new_state': 'Collected',
                                    'content_source': item_for_db_filtered_for_db.get('content_source'), # Should be 'external_webhook'
                                    'filled_by_file': item_for_db_filtered_for_db.get('filled_by_file') # ADDED this line
                                    # 'content_source_detail': os.path.basename(original_file_path_str) # REMOVED this line
                                }
                                if item_for_db_filtered_for_db.get('type') == 'episode':
                                    notification_item.update({
                                        'season_number': item_for_db_filtered_for_db.get('season_number'),
                                        'episode_number': item_for_db_filtered_for_db.get('episode_number'),
                                        'episode_title': item_for_db_filtered_for_db.get('episode_title')
                                    })
                                
                                logging.info(f"[RcloneScan {task_id}] Sending 'collected' notification for item: {notification_item.get('title')}")
                                send_notifications([notification_item], get_enabled_notifications(), notification_category='collected')
                            except Exception as notify_err:
                                logging.error(f"[RcloneScan {task_id}] Failed to send notification for {item_for_db_filtered_for_db.get('title')}: {notify_err}", exc_info=True)
                        # --- End Notification ---
                        
                    except Exception as e:
                        logging.error(f"[RcloneScan {task_id}] Symlink creation error for {symlink_dest_path} (DB ID {item_id_from_db}): {e}", exc_info=True)
                        symlink_errors += 1; error_list.append(f"Symlink Error ({item_path.name}): {e}"); update_progress(symlink_errors=symlink_errors, errors=error_list)
            update_progress(items_processed=items_processed)


        final_message_parts = [
            f"Rclone scan finished. Scanned: {total_files_scanned}, Media Files Initially Found: {media_files_found}, Items Chosen for Processing: {items_processed}."
        ]
        if dry_run:
            final_message_parts.append(f"Dry Run Preview: {len(preview_list)} items.")
        else:
             final_message_parts.append(f"DB Added: {items_added_to_db}, Symlinks Created: {symlinks_created}.")
        if skipped_previously_processed_count > 0: final_message_parts.append(f"Skipped (Previously Processed): {skipped_previously_processed_count}.")
        if skipped_due_to_size_count > 0: final_message_parts.append(f"Skipped (Movie Size Below Threshold): {skipped_due_to_size_count}.") # Updated counter
        if skipped_duplicates > 0: final_message_parts.append(f"Skipped (DB Duplicates): {skipped_duplicates}.")
        # ... (other error counts) ...
        error_counts_str = []
        if parser_errors > 0: error_counts_str.append(f"Parser: {parser_errors}")
        if metadata_errors > 0: error_counts_str.append(f"Metadata: {metadata_errors}")
        if db_errors > 0: error_counts_str.append(f"DB: {db_errors}")
        if symlink_errors > 0: error_counts_str.append(f"Symlink: {symlink_errors}")
        if error_counts_str: final_message_parts.append(f"Errors ({', '.join(error_counts_str)}).")

        final_message = " ".join(final_message_parts)
        success_status = (parser_errors == 0 and metadata_errors == 0 and db_errors == 0 and symlink_errors == 0)

        update_progress(
            status='complete', message=final_message, complete=True, success=success_status,
            total_files_scanned=total_files_scanned, media_files_found=media_files_found, 
            items_processed=items_processed, items_added_to_db=items_added_to_db,
            symlinks_created=symlinks_created, parser_errors=parser_errors,
            metadata_errors=metadata_errors, db_errors=db_errors, symlink_errors=symlink_errors,
            skipped_duplicates=skipped_duplicates, 
            skipped_due_to_size=skipped_due_to_size_count, # Updated counter
            skipped_previously_processed=skipped_previously_processed_count
        )

    except Exception as e:
        logging.error(f"[RcloneScan {task_id}] Critical error in Rclone scan task: {e}", exc_info=True)
        update_progress(status='error', message=f'Task failed: {e}', complete=True, success=False)
    finally:
        try:
             if symlink_base_path_setting_backup is not None:
                 set_setting('File Management', 'symlinked_files_path', symlink_base_path_setting_backup)
        except Exception as restore_err:
             logging.error(f"[RcloneScan {task_id}] Failed to restore symlink path setting: {restore_err}")
             # ... (append to errors in progress dict) ...
        # Save final progress one last time, e.g. if loop broke early
        if not dry_run: save_rclone_progress() 
        threading.Timer(300, lambda: rclone_scan_progress.pop(task_id, None)).start()
