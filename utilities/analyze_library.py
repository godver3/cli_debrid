import os
import subprocess
import time
import json
import sqlite3
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Assuming these can be imported based on project structure
# If running standalone, PYTHONPATH might need adjustment or use absolute imports if project is a package
from utilities.settings import get_setting
from routes.debug_routes import move_item_to_wanted
from utilities.plex_removal_cache import cache_plex_removal

ANALYSIS_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm"}
ANALYSIS_THREADS = 15  # Number of threads for file analysis
MEDIA_ANALYSIS_PROGRESS_JSON = "media_file_analysis_progress.json" # Stored in USER_DB_CONTENT
FILES_TO_ANALYZE_PER_RUN = 200

def is_symlink_valid_for_analysis(path):
    if os.path.islink(path):
        try:
            resolved = os.path.realpath(path)
            return os.path.exists(resolved), resolved
        except Exception: 
            return False, None
    return False, None

def is_file_playable_for_analysis(path, ffprobe_timeout=30, warmup_read_size=1024):
    if not path or not os.path.exists(path) or os.path.isdir(path):
        return False

    # Attempt a small read to "warm up" the file access, especially for remote mounts
    try:
        with open(path, 'rb') as f:
            f.read(warmup_read_size)
        logging.debug(f"Warmup read successful for {path}")
    except IOError:
        # If warmup read fails, file is likely inaccessible
        logging.warning(f"Warmup read failed for {path}. File might be inaccessible.")
        return False # Or, one could choose to still let ffprobe try, but failing here is safer.
    except Exception as e:
        # Other errors during warmup
        logging.warning(f"Warmup read exception for {path}: {e}. Proceeding to ffprobe.")
        # We can choose to proceed to ffprobe or return False.
        # Let's proceed, ffprobe is the main check.
        pass

    try:
        cmd = [
            "ffprobe", "-v", "error", "-count_frames",
            "-select_streams", "v:0",
            "-show_entries", "stream=nb_read_frames",
            "-read_intervals", "%+#1",
            "-of", "csv=p=0", path
        ]
        output = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=ffprobe_timeout).decode().strip()
        value = output.split(',')[0]
        if value.isdigit() and int(value) > 0:
            return True
    except subprocess.TimeoutExpired:
        logging.warning(f"ffprobe (frame count) timed out after {ffprobe_timeout}s for {path}")
    except Exception as e:
        # Log general errors, but don't make it too verbose for common ffprobe "failures" on non-media/corrupt files
        logging.debug(f"ffprobe (frame count) check failed for {path}: {e}")
        pass # Fall through to the duration check

    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path
        ]
        output = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=ffprobe_timeout).decode().strip()
        # Ensure output is not empty before attempting float conversion
        if output and float(output) > 0:
            return True
    except subprocess.TimeoutExpired:
        logging.warning(f"ffprobe (duration) timed out after {ffprobe_timeout}s for {path}")
    except Exception as e:
        logging.debug(f"ffprobe (duration) check failed for {path}: {e}")
    
    return False

def _process_broken_media_item_analysis(db_path, item_id, title, imdb_id, season_number, episode_number, version, item_type, episode_title_text, location_on_disk, symlink_abs_path_to_remove_if_bad):
    conn = None
    cursor = None
    action_taken = "error_processing"
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        logging.info(f"Processing broken item: ID {item_id}, Title: {title}, Version: {version}")

        clean_version = version.replace('*', '') if version else None
        remaining_items_with_version = 0

        if clean_version:
            query_params = [f"%{clean_version}%", item_id, imdb_id]
            base_query_select = "SELECT COUNT(*) FROM media_items WHERE state IN ('Collected', 'Upgrading') AND version LIKE ? AND id != ? "
            base_query_exists = """
                AND location_on_disk IS NOT NULL AND EXISTS (
                    SELECT 1 FROM media_items m2 
                    WHERE m2.id = media_items.id AND m2.location_on_disk IS NOT NULL
                    AND EXISTS (SELECT 1 FROM media_items m3 WHERE m3.location_on_disk = m2.location_on_disk AND m3.id = m2.id)
                )
            """

            if item_type == 'movie':
                query = base_query_select + "AND type = 'movie' AND imdb_id = ? " + base_query_exists
            elif item_type == 'episode':
                query = base_query_select + "AND type = 'episode' AND imdb_id = ? AND season_number = ? AND episode_number = ? " + base_query_exists
                query_params.extend([season_number, episode_number])
            else:
                query = None

            if query:
                cursor.execute(query, tuple(query_params))
                count_result = cursor.fetchone()
                if count_result:
                    remaining_items_with_version = count_result[0]
            logging.info(f"  Item ID {item_id}: Found {remaining_items_with_version} other items with version '{clean_version}'.")

        if clean_version and remaining_items_with_version == 0:
            logging.info(f"  Item ID {item_id}: Last of its version '{clean_version}'. Moving to Wanted state.")
            # move_item_to_wanted needs app context if it uses current_app, ensure it's set up or passed
            # For now, assuming move_item_to_wanted can be called directly if it handles its own DB.
            move_item_to_wanted(item_id, None) 
            action_taken = "moved_to_wanted"
        else:
            log_msg = f"  Item ID {item_id}: "
            log_msg += "No version information. " if not clean_version else f"Other items exist with version '{clean_version}'. "
            log_msg += "Deleting this item."
            logging.info(log_msg)
            cursor.execute("DELETE FROM media_items WHERE id = ?", (item_id,))
            conn.commit()
            action_taken = "deleted_from_db"

        if symlink_abs_path_to_remove_if_bad and os.path.lexists(symlink_abs_path_to_remove_if_bad):
            try:
                os.unlink(symlink_abs_path_to_remove_if_bad)
                logging.info(f"  Removed symlink: {symlink_abs_path_to_remove_if_bad}")
            except OSError as e:
                logging.error(f"  Failed to remove symlink {symlink_abs_path_to_remove_if_bad}: {e}")
        
        plex_ep_title = episode_title_text if item_type == 'episode' else None
        cache_plex_removal(title, location_on_disk, episode_title=plex_ep_title)
        logging.info(f"  Queued for Plex removal: '{title}' at '{location_on_disk}'")

    except Exception as e:
        logging.error(f"Error in _process_broken_media_item_analysis for item ID {item_id}: {e}", exc_info=True)
        if conn: conn.rollback()
    finally:
        if cursor: cursor.close()
        if conn: conn.close()
    return action_taken

def task_analyze_single_file_and_take_action(db_path, abs_file_path, relative_path_from_base, collection_type, collection_base_path):
    is_broken = False
    break_reason = ""
    symlink_to_remove_if_bad = None
    actual_media_path_to_play_check = None
    
    logging.debug(f"Analyzing [{collection_type}]: {abs_file_path}")

    if collection_type == 'Symlinked/Local':
        valid_symlink, resolved_target = is_symlink_valid_for_analysis(abs_file_path)
        if not valid_symlink:
            is_broken = True
            break_reason = "Invalid or broken symlink"
            symlink_to_remove_if_bad = abs_file_path 
        else:
            actual_media_path_to_play_check = resolved_target
    elif collection_type == 'Plex':
        actual_media_path_to_play_check = abs_file_path
        if os.path.islink(abs_file_path):
            valid_symlink, resolved_target = is_symlink_valid_for_analysis(abs_file_path)
            if not valid_symlink:
                 is_broken = True
                 break_reason = "Underlying symlink for Plex item is broken"
                 symlink_to_remove_if_bad = abs_file_path
            else:
                 actual_media_path_to_play_check = resolved_target

    if not is_broken and actual_media_path_to_play_check:
        if not is_file_playable_for_analysis(actual_media_path_to_play_check):
            is_broken = True
            break_reason = f"File not playable ({os.path.basename(actual_media_path_to_play_check)})"
            if collection_type == 'Symlinked/Local': # For symlinks, the symlink itself is the problem source
                 symlink_to_remove_if_bad = abs_file_path
    elif not is_broken and not actual_media_path_to_play_check and collection_type == 'Symlinked/Local':
        is_broken = True
        break_reason = "Symlink resolution issue prior to playability check"
        symlink_to_remove_if_bad = abs_file_path

    status = "ok"
    if is_broken:
        logging.warning(f"Item at '{abs_file_path}' (relative: '{relative_path_from_base}') is broken: {break_reason}")
        
        conn_thread = None
        cursor_thread = None
        try:
            conn_thread = sqlite3.connect(db_path)
            conn_thread.row_factory = sqlite3.Row
            cursor_thread = conn_thread.cursor()
            
            # Path for DB lookup is the absolute path for both Symlinked/Local and Plex,
            # based on the information that both store absolute paths in location_on_disk.
            path_for_db_lookup = abs_file_path
            # The relative_path_from_base is still useful for context in some logs or if other types behave differently.
            
            logging.debug(f"  Attempting DB lookup using absolute path: '{path_for_db_lookup}'")

            cursor_thread.execute("""
                SELECT id, title, imdb_id, season_number, episode_number, version, type, episode_title, original_path_for_symlink, location_on_disk
                FROM media_items 
                WHERE location_on_disk = ? AND state IN ('Collected', 'Upgrading')
            """, (path_for_db_lookup,))
            item_data = cursor_thread.fetchone()

            if item_data:
                logging.info(f"  DB item found for broken file: ID {item_data['id']}, Title: {item_data['title']}")
                # Use the path as it is in the DB for removal consistency.
                # Since we found the item using path_for_db_lookup (which is abs_file_path),
                # item_data['location_on_disk'] will be this absolute path.
                path_to_report_for_removal = item_data['location_on_disk'] 

                status = _process_broken_media_item_analysis(
                    db_path, item_data['id'], item_data['title'], item_data['imdb_id'],
                    item_data['season_number'], item_data['episode_number'], item_data['version'],
                    item_data['type'], item_data['episode_title'], path_to_report_for_removal,
                    symlink_to_remove_if_bad
                )
            else:
                # Updated logging to consistently reflect that we're using the absolute path for lookup.
                logging.warning(f"  No DB record found for broken file using absolute path '{path_for_db_lookup}'. It might be an orphaned file or already processed.")
                status = "db_record_not_found"
                if symlink_to_remove_if_bad and os.path.lexists(symlink_to_remove_if_bad) and collection_type == 'Symlinked/Local':
                    try:
                        os.unlink(symlink_to_remove_if_bad)
                        logging.info(f"  Removed orphaned broken symlink: {symlink_to_remove_if_bad}")
                        status = "orphaned_symlink_removed"
                    except OSError as e:
                        logging.error(f"  Failed to remove orphaned symlink {symlink_to_remove_if_bad}: {e}")
                        status = "error_removing_orphaned_symlink"
                        
        except Exception as e:
            logging.error(f"Error during DB interaction for broken file {abs_file_path}: {e}", exc_info=True)
            status = "error_db_interaction"
        finally:
            if cursor_thread: cursor_thread.close()
            if conn_thread: conn_thread.close()
    else:
        logging.debug(f"File ok: {abs_file_path}")
        
    return abs_file_path, status

def analyze_and_repair_media_files(collection_type, max_files_to_check_this_run=FILES_TO_ANALYZE_PER_RUN):
    """
    Main orchestrator for analyzing and repairing media files for a given collection type.
    Scans files starting from the last processed point, and resets when a full pass is completed.
    """
    logging.info(f"Starting media file analysis and repair for collection type: {collection_type}")

    db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    if not db_content_dir:
        logging.error("USER_DB_CONTENT environment variable not set. Cannot determine DB path.")
        return
    db_path = os.path.join(db_content_dir, 'media_items.db')
    progress_file_full_path = os.path.join(db_content_dir, MEDIA_ANALYSIS_PROGRESS_JSON)

    base_path = None
    if collection_type == 'Plex':
        base_path = get_setting('Plex', 'mounted_file_location')
    elif collection_type == 'Symlinked/Local':
        base_path = get_setting('File Management', 'symlinked_files_path')
    
    if not base_path:
        logging.error(f"Base path for {collection_type} not configured via settings. Aborting analysis.")
        return
    if not os.path.exists(base_path):
        logging.error(f"Base path '{base_path}' for {collection_type} does not exist. Aborting analysis.")
        return

    current_progress_data = {"last_processed_absolute_path": "", "total_files_analyzed_ever": 0}
    if os.path.exists(progress_file_full_path):
        try:
            with open(progress_file_full_path, 'r') as f:
                current_progress_data = json.load(f)
        except json.JSONDecodeError:
            logging.warning(f"Could not decode progress file {progress_file_full_path}. Resetting to defaults.")
            current_progress_data = {"last_processed_absolute_path": "", "total_files_analyzed_ever": 0}

    last_processed_path_from_file = current_progress_data.get("last_processed_absolute_path", "")
    total_analyzed_ever_start_of_run = current_progress_data.get("total_files_analyzed_ever", 0)
    
    logging.info(f"Attempting to resume analysis for {collection_type}. Last processed file: '{last_processed_path_from_file or 'None'}'")
    logging.info(f"Scanning '{base_path}' for the next batch of up to {max_files_to_check_this_run} eligible files...")

    files_to_process_this_run = []
    hit_max_files_limit_for_batch = False

    def file_walker(root_path): # Removed last_known_file, not needed with direct comparison
        for root, dirs, files in os.walk(root_path, topdown=True):
            dirs.sort()
            files.sort()
            for file_name in files:
                yield os.path.join(root, file_name)

    for abs_file_path in file_walker(base_path):
        if Path(abs_file_path).suffix.lower() not in ANALYSIS_VIDEO_EXTENSIONS:
            continue

        if abs_file_path > last_processed_path_from_file:
            files_to_process_this_run.append(abs_file_path)
            if len(files_to_process_this_run) >= max_files_to_check_this_run:
                hit_max_files_limit_for_batch = True
                break 

    logging.info(f"Identified {len(files_to_process_this_run)} files to process in this run for {collection_type}. Hit batch limit: {hit_max_files_limit_for_batch}")

    if not files_to_process_this_run and not hit_max_files_limit_for_batch:
        if last_processed_path_from_file: 
            logging.info(f"Full scan pass completed for {collection_type}, no new files found after '{last_processed_path_from_file}'. Resetting progress for next cycle.")
            current_progress_data["last_processed_absolute_path"] = ""
            try:
                with open(progress_file_full_path, 'w') as f:
                    json.dump(current_progress_data, f, indent=4)
                logging.info(f"Progress reset for {collection_type}. Next run will start from the beginning.")
            except IOError as e:
                logging.error(f"Could not save reset progress to {progress_file_full_path}: {e}")
            logging.info(f"Media file analysis and repair cycle for {collection_type} completed.")
            return

    if not files_to_process_this_run:
        logging.info(f"No files to process for {collection_type} in this run.")
        return

    files_submitted_for_analysis = 0
    futures = []
    
    with ThreadPoolExecutor(max_workers=max(1, ANALYSIS_THREADS)) as executor:
        for abs_file_path_to_analyze in files_to_process_this_run:
            relative_path = os.path.relpath(abs_file_path_to_analyze, base_path)
            future = executor.submit(task_analyze_single_file_and_take_action, db_path, abs_file_path_to_analyze, relative_path, collection_type, base_path)
            futures.append(future)
            files_submitted_for_analysis += 1
        
        logging.info(f"Submitted {files_submitted_for_analysis} files for analysis for {collection_type}.")

        processed_count_this_session = 0
        # Initialize to ensure it has a value before loop; actual value comes from processed files
        last_successfully_processed_path_in_batch = last_processed_path_from_file 
        if files_to_process_this_run: # If there are files, take the first as a baseline that will be updated
            last_successfully_processed_path_in_batch = files_to_process_this_run[0]


        temp_total_analyzed_this_session = 0

        for future in as_completed(futures):
            try:
                processed_abs_path, status = future.result()
                logging.info(f"Completed analysis for {processed_abs_path} ({collection_type}), status: {status}")
                
                if processed_abs_path > last_successfully_processed_path_in_batch:
                    last_successfully_processed_path_in_batch = processed_abs_path
                
                temp_total_analyzed_this_session += 1
                processed_count_this_session += 1

                if processed_count_this_session % 20 == 0 and processed_count_this_session < files_submitted_for_analysis:
                    intermediate_progress_to_save = {
                        "last_processed_absolute_path": last_successfully_processed_path_in_batch,
                        "total_files_analyzed_ever": total_analyzed_ever_start_of_run + temp_total_analyzed_this_session
                    }
                    try:
                        with open(progress_file_full_path, 'w') as f:
                            json.dump(intermediate_progress_to_save, f, indent=4)
                        logging.debug(f"Saved intermediate progress for {collection_type}. Last path: {last_successfully_processed_path_in_batch}")
                    except IOError as e:
                        logging.error(f"Could not save intermediate progress for {collection_type} to {progress_file_full_path}: {e}")
            except Exception as exc:
                logging.error(f'A file analysis task for {collection_type} generated an exception: {exc}', exc_info=True)
        
    current_progress_data["total_files_analyzed_ever"] = total_analyzed_ever_start_of_run + temp_total_analyzed_this_session

    if files_submitted_for_analysis > 0: # Only update path if we actually processed something
        if not hit_max_files_limit_for_batch:
            logging.info(f"Completed full scan pass for {collection_type}. Last processed file in this pass: {last_successfully_processed_path_in_batch}. Resetting for next cycle.")
            current_progress_data["last_processed_absolute_path"] = "" 
        else:
            current_progress_data["last_processed_absolute_path"] = last_successfully_processed_path_in_batch
    # If files_submitted_for_analysis is 0, progress_data path remains unchanged (handled by early exits)

    try:
        with open(progress_file_full_path, 'w') as f:
            json.dump(current_progress_data, f, indent=4)
        final_path_for_log = current_progress_data['last_processed_absolute_path'] or 'Beginning'
        logging.info(f"Final progress saved for {collection_type}. Last processed file for next run: '{final_path_for_log}'. Total analyzed ever: {current_progress_data['total_files_analyzed_ever']}")
    except IOError as e:
        logging.error(f"Could not save final progress for {collection_type} to {progress_file_full_path}: {e}")

    logging.info(f"Media file analysis and repair for {collection_type} completed. Processed {processed_count_this_session} files in this session.")