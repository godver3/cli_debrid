"""Symlink recovery: analysis, progress stream, recovery and path rewrite."""

from flask import jsonify, render_template, request, Response, stream_with_context
from utilities.settings import get_all_settings, get_setting
import logging
from routes import admin_required
import os
import time
from datetime import datetime
import json
import threading
import re
from pathlib import Path
from utilities.reverse_parser import parse_filename_for_version
import sqlite3
from ._common import analysis_progress, debug_bp

# --- End new route ---

# --- Symlink Recovery Routes ---

@debug_bp.route('/recover_symlinks')
@admin_required
def recover_symlinks_page():
    """Renders the symlink recovery page."""
    return render_template('recover_symlinks.html')

def parse_symlink(symlink_path: Path):
    """Parses a symlink path based on filename patterns, not templates."""
    filename = symlink_path.name
    parsed_data = {
        'symlink_path': str(symlink_path),
        'original_path_for_symlink': None, # Populated in analyze_symlinks
        'media_type': None, # Determined below
        'imdb_id': None, # Determined below
        'tmdb_id': None, # Populated by get_metadata
        'title': None, # Populated by get_metadata
        'year': None, # Populated by get_metadata
        'season_number': None, # Determined below
        'episode_number': None, # Determined below
        'episode_title': None, # Populated by get_metadata
        'version': None, # Populated by reverse_parser in analyze_symlinks
        'original_filename': None, # Populated in analyze_symlinks
        'is_anime': False # Populated by get_metadata
    }

    # 1. Extract IMDb ID (tt#######) - try filename first, then full path
    imdb_match = re.search(r'(tt\d{7,})', filename, re.IGNORECASE)
    if imdb_match:
        parsed_data['imdb_id'] = imdb_match.group(1)
    else:
        # Try searching in the full path as fallback
        full_path_str = str(symlink_path)
        imdb_match = re.search(r'(tt\d{7,})', full_path_str, re.IGNORECASE)
        if imdb_match:
            parsed_data['imdb_id'] = imdb_match.group(1)
            logging.info(f"Found IMDb ID in full path: {parsed_data['imdb_id']} for {filename}")
        else:
            logging.warning(f"Could not extract IMDb ID from filename or full path: {filename}")
            return None # Cannot proceed without IMDb ID

    # 2. Extract Season and Episode Numbers (S##E## or similar)
    # More robust regex to handle variations like S01E01, S1E1, Season 1 Episode 1, 1x01 etc.
    # Also handles multi-episode formats like S11E17-E18 or S11E17E18
    se_match = re.search(r'[Ss](\d{1,2})[EeXx](\d{1,3}(?:[EeXx]\d{1,3})*)|Season\s?(\d{1,2})\s?Episode\s?(\d{1,3})|(\d{1,2})[Xx](\d{1,3})', filename)
    if se_match:
        parsed_data['media_type'] = 'episode'
        # Extract numbers from the first matching group that isn't None
        if se_match.group(1) is not None and se_match.group(2) is not None:
            parsed_data['season_number'] = int(se_match.group(1))
            episode_match = se_match.group(2)
            # Handle multi-episode format (e.g., "17E18" or "17-18")
            if 'E' in episode_match or '-' in episode_match:
                # Extract all episode numbers and create a range format
                episode_numbers = re.findall(r'\d+', episode_match)
                if len(episode_numbers) > 1:
                    parsed_data['episode_number'] = f"E{episode_numbers[0]}-E{episode_numbers[-1]}"
                else:
                    parsed_data['episode_number'] = int(episode_numbers[0])
            else:
                parsed_data['episode_number'] = int(episode_match)
        elif se_match.group(3) is not None and se_match.group(4) is not None:
             parsed_data['season_number'] = int(se_match.group(3))
             parsed_data['episode_number'] = int(se_match.group(4))
        elif se_match.group(5) is not None and se_match.group(6) is not None:
             parsed_data['season_number'] = int(se_match.group(5))
             parsed_data['episode_number'] = int(se_match.group(6))
        else:
             logging.warning(f"Regex matched S/E pattern but failed to extract numbers for: {filename}")
             # Decide if this is fatal? Maybe still treat as movie?
             parsed_data['media_type'] = 'movie' # Fallback to movie if numbers aren't extracted
    else:
        parsed_data['media_type'] = 'movie'

    logging.debug(f"Parsed initial data from {filename}: IMDb={parsed_data['imdb_id']}, Type={parsed_data['media_type']}, S={parsed_data['season_number']}, E={parsed_data['episode_number']}")
    return parsed_data

def _run_analysis_thread(symlink_root_path_str, original_root_path_str, task_id):
    """The actual analysis logic, run in a background thread."""

    # --- Create a temporary directory for recovery files ---
    db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    temp_recovery_dir = os.path.join(db_content_dir, 'tmp_recovery')
    try:
        os.makedirs(temp_recovery_dir, exist_ok=True)
    except OSError as e:
        logging.error(f"Failed to create temporary recovery directory {temp_recovery_dir}: {e}")
        # Handle error: update progress and exit thread? For now, log and continue, recovery will fail later.
        pass 
    # --- End temporary directory creation ---

    # Generate a unique temporary file path for this task
    recovery_file_path = os.path.join(temp_recovery_dir, f"recovery_{task_id}.jsonl")

    analysis_progress[task_id] = {
        'status': 'starting',
        'message': 'Initializing analysis...',
        'total_items_scanned': 0,
        'total_symlinks_processed': 0,
        'total_files_processed': 0,
        'items_found': 0,
        'parser_errors': 0,
        'metadata_errors': 0,
        'recoverable_items_preview': [], # Keep the preview list
        # 'recoverable_items': [], # REMOVED - Will use file instead
        'recovery_file_path': None, # Will be set on completion
        'complete': False
    }

    def update_progress(**kwargs):
        if task_id in analysis_progress:
            analysis_progress[task_id].update(kwargs)
            # Limit the preview list size
            preview = analysis_progress[task_id]['recoverable_items_preview']
            if len(preview) > 5:
                 analysis_progress[task_id]['recoverable_items_preview'] = preview[:5]
        else:
            logging.warning(f"Task ID {task_id} not found in progress dict during update.")

    # Use a try/finally block to ensure file closure and cleanup logic
    recovery_file = None
    try:
        # Open the recovery file in append mode with UTF-8 encoding
        recovery_file = open(recovery_file_path, 'a', encoding='utf-8')

        symlink_root_path = Path(symlink_root_path_str)
        original_root_path = Path(original_root_path_str) if original_root_path_str else None

        if not symlink_root_path.is_dir():
            raise ValueError('Symlink Root Path must be a valid directory.')
        if original_root_path and not original_root_path.is_dir():
            raise ValueError('Original Root Path must be valid if provided.')

        # Read relevant settings
        symlink_folder_order_str = get_setting('File Management', 'symlink_folder_order', 'type,version,resolution')
        organize_by_type = get_setting('File Management', 'symlink_organize_by_type', True)
        organize_by_resolution = get_setting('File Management', 'symlink_organize_by_resolution', False)
        organize_by_version = get_setting('File Management', 'symlink_organize_by_version', False)
        
        separate_anime = get_setting('Debug', 'enable_separate_anime_folders', False)
        movies_folder_name = get_setting('Debug', 'movies_folder_name', 'Movies')
        tv_shows_folder_name = get_setting('Debug', 'tv_shows_folder_name', 'TV Shows')
        anime_movies_folder_name = get_setting('Debug', 'anime_movies_folder_name', 'Anime Movies')
        anime_tv_shows_folder_name = get_setting('Debug', 'anime_tv_shows_folder_name', 'Anime TV Shows')

        ignored_extensions = {'.srt', '.sub', '.idx', '.nfo', '.txt', '.jpg', '.png', '.db', '.partial', '.!qB'}
        
        folder_order_components = [comp.strip() for comp in symlink_folder_order_str.split(',')]
        component_map = {'type': [], 'resolution': [], 'version': []}

        if organize_by_type:
            if movies_folder_name: component_map['type'].append(movies_folder_name)
            if tv_shows_folder_name: component_map['type'].append(tv_shows_folder_name)
            if separate_anime:
                if anime_movies_folder_name: component_map['type'].append(anime_movies_folder_name)
                if anime_tv_shows_folder_name: component_map['type'].append(anime_tv_shows_folder_name)
        
        if organize_by_resolution:
            # Consistent with original code's typical resolution folder names
            component_map['resolution'] = ["2160p", "1080p", "720p", "SD"] 

        if organize_by_version:
            all_settings = get_all_settings() # Use get_all_settings to fetch nested dict
            scraping_settings = all_settings.get('Scraping', {})
            version_configs = scraping_settings.get('versions', {})
            configured_versions = [str(v).strip('*') for v in version_configs.keys() if str(v).strip('*')]
            if configured_versions:
                component_map['version'] = configured_versions
            else:
                component_map['version'].append("Default")


        # Build paths based on current settings
        paths_to_scan_tuples = [(symlink_root_path, symlink_root_path.name)] # (Path, description_for_logging)

        for component_key in folder_order_components:
            folders_for_this_component_type = component_map.get(component_key, [])
            
            is_component_active_for_path_building = False
            if component_key == 'type' and organize_by_type: is_component_active_for_path_building = True
            elif component_key == 'resolution' and organize_by_resolution: is_component_active_for_path_building = True
            elif component_key == 'version' and organize_by_version: is_component_active_for_path_building = True

            if is_component_active_for_path_building and folders_for_this_component_type:
                current_level_new_paths = []
                for base_path_obj, base_desc_str in paths_to_scan_tuples:
                    for folder_segment_name in folders_for_this_component_type:
                        if folder_segment_name: # Ensure folder_segment_name is not None or empty
                            current_level_new_paths.append(
                                (base_path_obj / folder_segment_name, f"{base_desc_str}/{folder_segment_name}")
                            )
                if current_level_new_paths: 
                    paths_to_scan_tuples = current_level_new_paths

        # Add comprehensive fallback scanning - scan the entire symlink root recursively
        # This ensures we don't miss items that don't match the current settings
        comprehensive_scan_path = (symlink_root_path, f"{symlink_root_path.name} (comprehensive)")
        if comprehensive_scan_path not in paths_to_scan_tuples:
            paths_to_scan_tuples.append(comprehensive_scan_path)
        
        total_items_scanned = 0
        total_symlinks_processed = 0
        total_files_processed = 0
        items_found = 0
        parser_errors = 0
        metadata_errors = 0
        recoverable_items_preview = []

        update_progress(status='scanning', message='Starting directory scan...')
        
        # Log what paths we're going to scan
        scan_paths_info = [f"{desc}: {path}" for path, desc in paths_to_scan_tuples]
        logging.info(f"Analysis will scan the following paths: {scan_paths_info}")

        for current_search_path, scan_target_name in paths_to_scan_tuples:
            if current_search_path.is_dir():
                update_progress(message=f'Scanning {scan_target_name}...')
                try:
                    # Use rglob to scan recursively within the target directory
                    for item_path in current_search_path.rglob('*'):
                        total_items_scanned += 1
                        if total_items_scanned % 100 == 0: # Update progress periodically
                            update_progress(
                                total_items_scanned=total_items_scanned,
                                total_symlinks_processed=total_symlinks_processed,
                                total_files_processed=total_files_processed,
                                items_found=items_found,
                                parser_errors=parser_errors,
                                metadata_errors=metadata_errors,
                                message=f'Scanned {total_items_scanned} items...'
                                )

                        if item_path.suffix.lower() in ignored_extensions:
                            continue

                        if item_path.is_file() or item_path.is_symlink():
                            # Log some items for debugging (but not too many)
                            if total_items_scanned % 1000 == 0:
                                logging.debug(f"Scanning item: {item_path}")
                            if item_path.is_symlink():
                                total_symlinks_processed += 1
                            else:
                                total_files_processed += 1

                            parsed_data = parse_symlink(item_path)
                            if not parsed_data:
                                parser_errors += 1
                                continue # Skip if initial parse fails

                            # --- Determine original path and filename ---
                            original_path_obj = None
                            if item_path.is_symlink():
                                try:
                                    target_path_str = os.readlink(str(item_path))
                                    if not os.path.isabs(target_path_str):
                                        target_path_str = os.path.abspath(os.path.join(item_path.parent, target_path_str))
                                    original_path_obj = Path(target_path_str)
                                except Exception as e:
                                    parsed_data['original_path_for_symlink'] = f"Error: Cannot read link target ({e})"
                                    parsed_data['original_filename'] = item_path.name
                            elif item_path.is_file():
                                original_path_obj = item_path

                            if original_path_obj and original_path_obj.is_file():
                                parsed_data['original_path_for_symlink'] = str(original_path_obj)
                                parsed_data['original_filename'] = original_path_obj.name
                            elif 'original_path_for_symlink' not in parsed_data:
                                if original_path_obj:
                                        parsed_data['original_path_for_symlink'] = f"Error: Target not a file ({original_path_obj})"
                                else:
                                    parsed_data['original_path_for_symlink'] = "Error: Original path unknown"
                                parsed_data['original_filename'] = item_path.name
                            # --- End original path determination ---

                            # --- Get version ---    
                            filename_for_version = parsed_data.get('original_filename')
                            if filename_for_version:
                                try:
                                    version_raw = parse_filename_for_version(filename_for_version)
                                    parsed_data['version'] = version_raw.strip('*') if version_raw else 'Default'
                                except Exception as e:
                                    parsed_data['version'] = 'Default'
                            else:
                                parsed_data['version'] = 'Default'
                            # --- End version --- 
                                
                            # --- Fetch metadata --- 
                            if parsed_data['imdb_id']:
                                metadata_args = {
                                    'imdb_id': parsed_data['imdb_id'],
                                    'item_media_type': parsed_data.get('media_type') # Re-add based on function signature
                                }
                                # Removed conditional adding of season/episode number
                                # get_metadata likely handles this internally based on imdb_id
                                try:
                                    # Pass the original parsed_data as original_item if needed by get_metadata internal logic
                                    metadata_args['original_item'] = parsed_data 
                                    from metadata.metadata import get_metadata
                                    metadata = get_metadata(**metadata_args)
                                    if metadata:
                                        parsed_data['title'] = metadata.get('title')
                                        parsed_data['year'] = metadata.get('year')
                                        # Update tmdb_id if get_metadata found it
                                        parsed_data['tmdb_id'] = metadata.get('tmdb_id') or parsed_data.get('tmdb_id')
                                        parsed_data['release_date'] = metadata.get('release_date')
                                        if parsed_data['media_type'] == 'episode':
                                            parsed_data['episode_title'] = metadata.get('episode_title')
                                        genres = metadata.get('genres', [])
                                        if isinstance(genres, list):
                                            parsed_data['is_anime'] = any(g.lower() in ['animation', 'anime'] for g in genres)
                                        
                                        items_found += 1
                                        
                                        # Log successful item found
                                        if items_found % 100 == 0:  # Log every 100th item to avoid spam
                                            logging.info(f"Found item #{items_found}: {parsed_data.get('title', 'Unknown')} ({parsed_data.get('imdb_id', 'No IMDb')})")
                                        
                                        # --- Write item to recovery file ---
                                        try:
                                            recovery_file.write(json.dumps(parsed_data) + '\n')
                                        except Exception as write_err:
                                            logging.error(f"Error writing item to recovery file {recovery_file_path}: {write_err}")
                                            # Maybe mark the task as failed? For now, log and continue.
                                            # update_progress(status='error', message=f'Error writing recovery file: {write_err}')
                                        # --- End write item ---

                                        # Update preview list for UI feedback during scan
                                        if len(recoverable_items_preview) < 5:
                                                recoverable_items_preview.append(parsed_data)
                                        
                                        # Update progress (only preview list is stored in memory now)
                                        update_progress(items_found=items_found, recoverable_items_preview=recoverable_items_preview)
                                    else:
                                        metadata_errors += 1
                                except Exception as e:
                                    logging.error(f"Metadata error for {metadata_args}: {e}", exc_info=False) # Less verbose logging
                                    metadata_errors += 1
                        else: # No IMDb ID was parsed
                            parser_errors += 1 # This case should be caught by parse_symlink returning None now
                            # Log parser errors but limit frequency to avoid spam
                            if parser_errors % 50 == 0:  # Log every 50th parser error
                                logging.warning(f"Parser error #{parser_errors}: Skipping {item_path.name} as no IMDb ID was parsed")

                except Exception as e_rglob:
                    logging.error(f"Error during rglob scan of {current_search_path}: {e_rglob}", exc_info=True)
            else:
                logging.warning(f"Directory not found or not accessible: {current_search_path} (derived from order: {symlink_folder_order_str})")
                    
        # Analysis complete, update final status and store recovery file path
        update_progress(
            status='complete',
            message='Analysis finished.',
            complete=True,
            # recoverable_items=analysis_progress[task_id]['recoverable_items'], # REMOVED
            recovery_file_path=recovery_file_path if items_found > 0 else None, # Store file path if items were found
            total_items_scanned=total_items_scanned,
            total_symlinks_processed=total_symlinks_processed,
            total_files_processed=total_files_processed,
            items_found=items_found,
            parser_errors=parser_errors,
            metadata_errors=metadata_errors
        )

    except Exception as e:
        logging.error(f"Analysis thread error for task {task_id}: {e}", exc_info=True)
        update_progress(status='error', message=f'Analysis failed: {e}', complete=True)
    finally:
        if recovery_file:
            try:
                recovery_file.close()
            except Exception as close_err:
                 logging.error(f"Error closing recovery file {recovery_file_path}: {close_err}")
        pass

@debug_bp.route('/analyze_symlinks', methods=['POST'])
@admin_required
def analyze_symlinks():
    """Initiates the symlink analysis in a background thread and returns a task ID."""
    import uuid
    symlink_root_path_str = request.form.get('symlink_root_path')
    original_root_path_str = request.form.get('original_root_path')

    if not symlink_root_path_str:
        return jsonify({'success': False, 'error': 'Symlink Root Path is required.'}), 400

    task_id = str(uuid.uuid4())
    
    # Start analysis in background thread
    thread = threading.Thread(
        target=_run_analysis_thread, 
        args=(symlink_root_path_str, original_root_path_str, task_id)
    )
    thread.daemon = True
    thread.start()

    return jsonify({'success': True, 'task_id': task_id}) # Return task ID for progress tracking

@debug_bp.route('/analysis_progress/<task_id>')
def analysis_progress_stream(task_id):
    """SSE endpoint for tracking analysis progress."""
    def generate():
        while True:
            if task_id not in analysis_progress:
                progress = {'status': 'error', 'message': 'Task not found or expired', 'complete': True}
                yield f"data: {json.dumps(progress)}\n\n"
                break
                
            progress = analysis_progress[task_id]
            yield f"data: {json.dumps(progress)}\n\n"
            
            if progress.get('complete', False):
                # Maybe remove from dict after sending final status?
                # analysis_progress.pop(task_id, None) 
                break
                
            time.sleep(1) # Poll interval
            
    return Response(stream_with_context(generate()), mimetype='text/event-stream')

@debug_bp.route('/perform_recovery', methods=['POST'])
@admin_required
def perform_recovery():
    """Recovers all items found during a specific analysis task by reading from its recovery file."""
    from database import add_media_item

    data = request.get_json()
    task_id = data.get('task_id')

    if not task_id:
        return jsonify({'success': False, 'error': 'Missing task_id.'}), 400

    # Retrieve the analysis results (contains the file path)
    if task_id not in analysis_progress or not analysis_progress[task_id].get('complete'):
        return jsonify({'success': False, 'error': f'Analysis task {task_id} not found or not complete.'}), 404

    analysis_result = analysis_progress[task_id]
    recovery_file_path = analysis_result.get('recovery_file_path')
    expected_items = analysis_result.get('items_found', 0) # Get expected count

    if not recovery_file_path:
        # Check if items_found was 0, meaning no file was expected
        if expected_items == 0:
             return jsonify({'success': True, 'message': 'Analysis found no items to recover.', 'successful_recoveries': 0, 'failed_recoveries': 0}), 200
        else:
            return jsonify({'success': False, 'error': f'Recovery file path not found for completed task {task_id}. Analysis might have failed partially?'}), 404

    if not os.path.exists(recovery_file_path):
         return jsonify({'success': False, 'error': f'Recovery file not found at {recovery_file_path}. It might have been deleted or analysis failed.'}), 404

    # Removed: conn = None - add_media_item handles its own connection
    recovery_file = None
    successful_recoveries = 0
    failed_recoveries = 0
    errors = []
    COMMIT_BATCH_SIZE = 500 # Commit every 500 items - Note: add_media_item commits individually

    try:
        # Removed: conn = get_db_connection()
        recovery_file = open(recovery_file_path, 'r', encoding='utf-8')

        logging.info(f"Starting recovery from file: {recovery_file_path} for task {task_id}")

        for line_num, line in enumerate(recovery_file):
            item_data = None # Reset for each line
            try:
                line = line.strip()
                if not line: # Skip empty lines
                    continue

                item_data = json.loads(line)

                now_iso = datetime.now()

                # Prepare data ONLY with valid DB columns
                db_item_for_insert = {
                    'imdb_id': item_data.get('imdb_id'),
                    'tmdb_id': item_data.get('tmdb_id'),
                    'title': item_data.get('title'),
                    'year': item_data.get('year'),
                    'release_date': item_data.get('release_date'),
                    'state': 'Collected', # Mark as collected
                    'type': item_data.get('media_type'), # Source key is 'media_type', DB column is 'type'
                    'season_number': item_data.get('season_number'),
                    'episode_number': item_data.get('episode_number'),
                    'episode_title': item_data.get('episode_title'),
                    'collected_at': now_iso,
                    'original_collected_at': now_iso,
                    'original_path_for_symlink': item_data.get('original_path_for_symlink'),
                    'version': item_data.get('version', 'Default'),
                    'filled_by_file': item_data.get('original_filename'),
                    # 'last_updated': now_iso, # Let add_media_item handle this
                    'metadata_updated': now_iso,
                    'wake_count': 0,
                    # 'attempts': 0, # Removed - Not a DB column
                    # 'is_anime': item_data.get('is_anime', False), # Removed - Not a DB column (trigger_is_anime exists but not populated here)
                    'location_on_disk': item_data.get('symlink_path')
                    # Note: 'manually_added' and 'date_added' were also removed as they are not DB columns
                }

                # Filter out None values before passing to db function
                db_item_filtered = {k: v for k, v in db_item_for_insert.items() if v is not None}

                # Validate essential keys after filtering
                if not db_item_filtered.get('imdb_id') or not db_item_filtered.get('type'):
                    raise ValueError(f"Missing essential data (imdb_id or type) after filtering")

                # Call add_media_item and handle potential IntegrityError
                try:
                    # Pass the explicitly constructed and filtered dictionary.
                    # user_initiated=True: an admin explicitly triggered this
                    # recovery, so a ghostlisted/blacklisted row should be
                    # adopted (and unghosted) rather than silently refused --
                    # the same reasoning as the external-add import path.
                    item_id = add_media_item(db_item_filtered, user_initiated=True)
                    if item_id:
                        successful_recoveries += 1
                    else:
                        # add_media_item returning None suggests an issue other than IntegrityError
                        raise Exception("add_media_item failed to return an ID")
                except sqlite3.IntegrityError:
                     failed_recoveries += 1
                     item_desc = f"item on line {line_num + 1} (Path: {item_data.get('symlink_path', 'Unknown')})"
                     error_msg = f"Skipped recovery for {item_desc}: Item likely already exists (UNIQUE constraint violation)."
                     errors.append(error_msg)
                     logging.warning(error_msg) # Log as warning, not error

            except json.JSONDecodeError as json_err:
                 failed_recoveries += 1
                 error_msg = f"Failed to parse JSON on line {line_num + 1}: {json_err}"
                 errors.append(error_msg)
                 logging.error(error_msg)
            except ValueError as val_err:
                failed_recoveries += 1
                item_desc = f"item on line {line_num + 1} (Path: {item_data.get('symlink_path', 'Unknown') if item_data else 'Unknown'})"
                error_msg = f"Validation error for {item_desc}: {val_err}"
                errors.append(error_msg)
                logging.error(error_msg)
            except Exception as e:
                failed_recoveries += 1
                item_desc = f"item on line {line_num + 1} (Path: {item_data.get('symlink_path', 'Unknown') if item_data else 'Unknown'})"
                error_msg = f"Failed to recover {item_desc}: {str(e)}"
                errors.append(error_msg)
                logging.error(error_msg, exc_info=True) # Log full trace for unexpected errors

        # Removed final commit logic
        logging.info(f"Recovery processing complete for task {task_id}. Total successful: {successful_recoveries}, Failed/Skipped: {failed_recoveries}")

    except Exception as outer_err:
        # Error opening file or other outer-level issues
        error_msg = f"Error during recovery process: {str(outer_err)}"
        errors.append(error_msg)
        logging.error(error_msg, exc_info=True)
        # Can't determine success/failure counts accurately here, maybe set failed to expected?
        failed_recoveries = expected_items - successful_recoveries # Estimate failures
    finally:
        if recovery_file:
            try:
                recovery_file.close()
            except Exception as close_err:
                 logging.error(f"Error closing recovery file {recovery_file_path}: {close_err}")
        # Removed: conn close logic

        # Clean up the recovery file only if there were no errors during the file processing/db interaction
        if recovery_file_path and os.path.exists(recovery_file_path) and not errors:
            try:
                os.remove(recovery_file_path)
                logging.info(f"Successfully deleted recovery file: {recovery_file_path}")
            except Exception as del_err:
                logging.error(f"Failed to delete recovery file {recovery_file_path}: {del_err}")
                # Add a note about manual deletion maybe?
                errors.append(f"Note: Failed to automatically delete recovery file {os.path.basename(recovery_file_path)}. Please delete it manually.")
        elif errors:
             logging.warning(f"Recovery file {recovery_file_path} was not deleted due to errors during the recovery process.")
             errors.append(f"Note: Recovery file {os.path.basename(recovery_file_path)} was kept due to errors. Please review and delete it manually.")


    return jsonify({
        'success': failed_recoveries == 0, # Success only if no errors/skips? Or should skipped items be okay? Let's stick with failed_recoveries == 0 for now.
        'successful_recoveries': successful_recoveries,
        'failed_recoveries': failed_recoveries, # Includes skipped items due to IntegrityError
        'errors': errors
    })

# --- End Symlink Recovery Routes ---

# --- Symlink Path Modification --- 
@debug_bp.route('/api/modify_symlink_paths', methods=['POST'])
@admin_required
def modify_symlink_paths():
    """API endpoint to modify base paths for symlinks and original files in the database."""
    current_symlink_base = request.form.get('current_symlink_base', '').strip()
    new_symlink_base = request.form.get('new_symlink_base', '').strip()
    current_original_base = request.form.get('current_original_base', '').strip()
    new_original_base = request.form.get('new_original_base', '').strip()
    dry_run = request.form.get('dry_run') == 'on'

    modify_symlink = bool(current_symlink_base and new_symlink_base)
    modify_original = bool(current_original_base and new_original_base)

    if not modify_symlink and not modify_original:
        return jsonify({'success': False, 'error': 'Please provide at least one pair of current and new base paths to modify.'}), 400

    logging.info(f"Symlink path modification requested. Dry run: {dry_run}")
    if modify_symlink: logging.info(f"  Symlink: '{current_symlink_base}' -> '{new_symlink_base}'")
    if modify_original: logging.info(f"  Original: '{current_original_base}' -> '{new_original_base}'")

    from database import get_db_connection
    conn = None
    items_to_update = []
    preview_items = [] # For dry run
    MAX_PREVIEW = 10

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Query items that might need updating
        query = "SELECT id, location_on_disk, original_path_for_symlink FROM media_items WHERE "
        conditions = []
        params = []
        if modify_symlink:
            conditions.append("location_on_disk LIKE ?")
            params.append(current_symlink_base + '%')
        if modify_original:
            conditions.append("original_path_for_symlink LIKE ?")
            params.append(current_original_base + '%')
        
        query += " OR ".join(conditions)
        cursor.execute(query, params)
        items = cursor.fetchall()

        logging.info(f"Found {len(items)} potentially matching items in the database.")

        for item in items:
            item_id = item['id']
            current_location = item['location_on_disk']
            current_original = item['original_path_for_symlink']
            new_location = current_location
            new_original = current_original
            updated = False

            if modify_symlink and current_location and current_location.startswith(current_symlink_base):
                new_location = current_location.replace(current_symlink_base, new_symlink_base, 1)
                updated = True
                logging.debug(f"Item {item_id}: Symlink change '{current_location}' -> '{new_location}'")

            if modify_original and current_original and current_original.startswith(current_original_base):
                new_original = current_original.replace(current_original_base, new_original_base, 1)
                updated = True
                logging.debug(f"Item {item_id}: Original change '{current_original}' -> '{new_original}'")
            
            if updated:
                update_data = {
                    'id': item_id,
                    'new_location': new_location,
                    'new_original': new_original
                }
                items_to_update.append(update_data)
                if dry_run and len(preview_items) < MAX_PREVIEW:
                    preview_items.append({
                        'id': item_id,
                        'old_location': current_location,
                        'new_location': new_location,
                        'old_original': current_original,
                        'new_original': new_original
                    })

        logging.info(f"Identified {len(items_to_update)} items for potential update.")

        if dry_run:
            return jsonify({
                'success': True,
                'dry_run': True,
                'message': f"Dry run complete. Found {len(items_to_update)} items to update.",
                'items_to_update_count': len(items_to_update),
                'preview': preview_items
            })
        else:
            # Perform actual updates
            updated_count = 0
            if items_to_update:
                update_sql = "UPDATE media_items SET location_on_disk = ?, original_path_for_symlink = ? WHERE id = ?"
                # Prepare data for executemany
                update_params = [
                    (item['new_location'], item['new_original'], item['id'])
                    for item in items_to_update
                ]
                cursor.executemany(update_sql, update_params)
                conn.commit()
                updated_count = cursor.rowcount # Note: executemany rowcount might be unreliable on some drivers/dbs
                # Fetch actual count as fallback 
                if updated_count == -1 or updated_count is None:
                    updated_count = len(items_to_update)
                    
                logging.info(f"Successfully updated {updated_count} items in the database.")
                message = f"Successfully updated {updated_count} items."
            else:
                 message = "No items required updating based on the provided paths."

            return jsonify({
                'success': True,
                'dry_run': False,
                'message': message,
                'updated_count': updated_count
            })

    except Exception as e:
        logging.error(f"Error modifying symlink paths: {e}", exc_info=True)
        if conn and not dry_run: # Rollback if it wasn't a dry run
            try:
                conn.rollback()
            except Exception as rb_err:
                 logging.error(f"Rollback failed: {rb_err}")
        return jsonify({'success': False, 'error': f'An error occurred: {str(e)}'}), 500
    finally:
        if conn:
            conn.close()
