from flask import jsonify, request, render_template, session, flash, Blueprint, current_app
import sqlite3
import string
import logging
from sqlalchemy import text, inspect
from routes.extensions import db
from utilities.settings import get_setting
import json
from utilities.reverse_parser import get_version_settings, get_default_version, get_version_order, parse_filename_for_version
from .models import admin_required
from utilities.plex_removal_cache import cache_plex_removal
from utilities.plex_functions import remove_file_from_plex
from database.database_reading import get_media_item_by_id
import os
from datetime import datetime
from time import sleep
import time # Added for caching
from utilities.phalanx_db_cache_manager import PhalanxDBClassManager
from database.torrent_tracking import get_torrent_history
from utilities.web_scraper import get_media_meta
from queues.config_manager import get_content_source_display_names, load_config
from database import update_media_item_state
from utilities.local_library_scan import convert_item_to_symlink
from database.database_writing import update_media_item
# import math # Removed unused import
database_bp = Blueprint('database', __name__)

# Module-level cache for statistics
cached_stats_data = None
stats_cache_timestamp = 0
STATS_CACHE_DURATION_SECONDS = 60  # Cache statistics for 60 seconds

def get_item_size_gb(location_on_disk, original_path_for_symlink):
    file_path_to_check = None
    if original_path_for_symlink:
        try:
            if os.path.exists(original_path_for_symlink):
                file_path_to_check = original_path_for_symlink
        except Exception: # Handle potential errors with long paths, permissions etc.
            pass # Fall through to location_on_disk or return 0

    if not file_path_to_check and location_on_disk:
        try:
            if os.path.exists(location_on_disk):
                file_path_to_check = location_on_disk
        except Exception:
            pass

    if file_path_to_check:
        try:
            size_bytes = os.path.getsize(file_path_to_check)
            return round(size_bytes / (1024 * 1024 * 1024), 2)  # GB with 2 decimal places
        except OSError:
            logging.debug(f"OSError getting size for {file_path_to_check}")
            return 0.0
        except Exception as e:
            logging.debug(f"Unexpected error getting size for {file_path_to_check}: {e}")
            return 0.0
    return 0.0

# ---------------------------------------------------------------------------
# Lightweight statistics helper – counts collected movies / shows / episodes
# ---------------------------------------------------------------------------

def get_basic_collection_counts():
    """Return basic collection statistics.

    Mirrors the logic in `database.statistics.get_collected_counts` but stripped
    down to the three numbers we need, avoiding the summary table checks and
    extra overhead. This still honours the business rules of counting only
    collected / upgrading items, deduplicating movies by `imdb_id`, shows by
    episode `imdb_id`, and episodes by the (imdb_id, season, episode) tuple.
    """

    from database import get_db_connection

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Unique collected movies
        cursor.execute(
            """
            SELECT COUNT(DISTINCT imdb_id)
            FROM media_items
            WHERE type = 'movie' AND state IN ('Collected', 'Upgrading')
            """
        )
        total_movies = cursor.fetchone()[0]

        # Unique shows (distinct imdb_id among collected episodes)
        cursor.execute(
            """
            SELECT COUNT(DISTINCT imdb_id)
            FROM media_items
            WHERE type = 'episode' AND state IN ('Collected', 'Upgrading')
            """
        )
        total_shows = cursor.fetchone()[0]

        # Unique episodes (distinct imdb_id + season + episode)
        cursor.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT DISTINCT imdb_id, season_number, episode_number
                FROM media_items
                WHERE type = 'episode' AND state IN ('Collected', 'Upgrading')
            )
            """
        )
        total_episodes = cursor.fetchone()[0]

        return {
            'total_movies': total_movies,
            'total_shows': total_shows,
            'total_episodes': total_episodes,
        }
    finally:
        if conn:
            conn.close()

@database_bp.route('/', methods=['GET', 'POST'])
@admin_required
def index():
    request_start_time = time.perf_counter() # Start timer for the whole request
    timings = {'overall_start': request_start_time}
    logging.info(f"Database index route started. Request method: {request.method}, Args: {request.args}")
    global cached_stats_data, stats_cache_timestamp # Allow modification of module-level cache variables
    
    data = {
        'items': [],
        'all_columns': [], # Will be populated with DB columns + 'size'
        'selected_columns': [], # User's selection for display, validated
        'filters': [],
        'sort_column': 'id',
        'sort_order': 'asc',
        'alphabet': list(string.ascii_uppercase),
        'current_letter': 'A',
        'content_type': 'movie',
        'filter_logic': 'AND',
        'column_values': {},
        'operators': [
            {'value': 'contains', 'label': 'Contains'},
            {'value': 'equals', 'label': 'Equals'},
            {'value': 'not_equals', 'label': 'Not Equals'},
            {'value': 'starts_with', 'label': 'Starts With'},
            {'value': 'ends_with', 'label': 'Ends With'},
            {'value': 'greater_than', 'label': 'Greater Than'},
            {'value': 'less_than', 'label': 'Less Than'},
            {'value': 'is_null', 'label': 'Is Null'},
            {'value': 'is_not_null', 'label': 'Is Not Null'}
        ],
        'content_source_display_map': {}
    }

    # Get collection counts (with caching)
    current_time = time.time()
    if cached_stats_data and (current_time - stats_cache_timestamp < STATS_CACHE_DURATION_SECONDS):
        logging.info("Using cached collection counts.")
        counts = cached_stats_data
    else:
        logging.info("Fetching fresh collection counts (quick query).")
        counts = get_basic_collection_counts()
        cached_stats_data = counts
        stats_cache_timestamp = current_time
        logging.info(f"Collection counts cached for {STATS_CACHE_DURATION_SECONDS} seconds.")
    timings['stats_fetched'] = time.perf_counter()

    data['stats'] = {
        'total_movies': counts['total_movies'],
        'total_shows': counts['total_shows'],
        'total_episodes': counts['total_episodes']
    }

    try:
        content_source_display_map = get_content_source_display_names()
        data['content_source_display_map'] = content_source_display_map
    except Exception as e:
        logging.error(f"Error fetching content source display names: {e}")
        content_source_display_map = {}

    conn = None
    try:
        from database import get_db_connection
        conn = get_db_connection()
        cursor = conn.cursor()
        timings['db_connection_established'] = time.perf_counter()

        cursor.execute("PRAGMA table_info(media_items)")
        db_actual_columns = [column[1] for column in cursor.fetchall()]
        timings['table_info_fetched'] = time.perf_counter()
        
        all_columns_for_ui = db_actual_columns[:]
        if 'size' not in all_columns_for_ui:
            all_columns_for_ui.append('size')
        data['all_columns'] = all_columns_for_ui

        default_display_columns = ['id', 'title', 'year', 'type', 'state', 'version', 'size']

        # 1. Get raw selected columns (from POST or session/GET)
        raw_selected_columns = []
        if request.method == 'POST':
            raw_selected_columns = request.form.getlist('columns')
            session['selected_columns'] = raw_selected_columns
        else:
            selected_columns_json = request.args.get('selected_columns')
            if selected_columns_json:
                try:
                    raw_selected_columns = json.loads(selected_columns_json)
                except json.JSONDecodeError:
                    raw_selected_columns = session.get('selected_columns', [])
            else:
                raw_selected_columns = session.get('selected_columns', [])

        # 2. Get filter, sort, and other parameters from the request *NOW*
        filters = []
        filter_data_json = request.args.get('filters', '')
        if filter_data_json:
            try:
                filters = json.loads(filter_data_json)
            except json.JSONDecodeError:
                filters = []
        
        sort_column_req = request.args.get('sort_column', 'id') # Defined HERE
        sort_order_req = request.args.get('sort_order', 'asc').lower()
        content_type_req = request.args.get('content_type')
        current_letter_req = request.args.get('letter')
        filter_logic = request.args.get('filter_logic', 'AND').upper()

        # 3. Determine current selected columns for display
        current_selected_columns_for_display = [col for col in raw_selected_columns if col in all_columns_for_ui]
        if not current_selected_columns_for_display:
            current_selected_columns_for_display = [col for col in default_display_columns if col in all_columns_for_ui]
            if not current_selected_columns_for_display and 'id' in all_columns_for_ui:
                 current_selected_columns_for_display = ['id']
            elif not current_selected_columns_for_display: # Absolute fallback
                 current_selected_columns_for_display = [all_columns_for_ui[0]] if all_columns_for_ui else []

        # 4. Conditionally add 'size' to display if sorting by it (using the now-defined sort_column_req)
        if sort_column_req == 'size' and 'size' not in current_selected_columns_for_display: # Used HERE
            current_selected_columns_for_display.append('size')
            # Ensure it's a valid column (it should be, as it's in all_columns_for_ui)
            current_selected_columns_for_display = [col for col in current_selected_columns_for_display if col in all_columns_for_ui]

        # 5. Update the data dictionary with the final selected columns for the template
        data['selected_columns'] = current_selected_columns_for_display
        timings['column_processing_done'] = time.perf_counter()

        # Validate filter_logic, sort_order_req, and sort_column_req (now that it's defined)
        if filter_logic not in ['AND', 'OR']: filter_logic = 'AND'
        if sort_order_req not in ['asc', 'desc']: sort_order_req = 'asc'
        if sort_column_req not in all_columns_for_ui: # Validate against all UI-knowable columns
            sort_column_req = 'id' 
        
        data['sort_column'] = sort_column_req # Store validated sort column for the template
        data['sort_order'] = sort_order_req   # Store validated sort order for the template
        
        # Continue with setting up SQL query columns
        columns_for_sql_query = set(['id']) 
        for col in current_selected_columns_for_display: # Use the finalized display columns
            if col != 'size' and col in db_actual_columns:
                columns_for_sql_query.add(col)
        
        needs_size_data = (sort_column_req == 'size' or 'size' in current_selected_columns_for_display)
        if needs_size_data:
            if 'location_on_disk' in db_actual_columns:
                columns_for_sql_query.add('location_on_disk')
            if 'original_path_for_symlink' in db_actual_columns:
                columns_for_sql_query.add('original_path_for_symlink')
        
        # Ensure 'content_source' is fetched if filtering by it, and it's a DB column
        if 'content_source' in db_actual_columns and any(f.get('column') == 'content_source' for f in filters):
            columns_for_sql_query.add('content_source')

        final_columns_for_sql_query_list = list(columns_for_sql_query)
        columns_quoted_str = ', '.join([f'"{col}"' for col in final_columns_for_sql_query_list])
        base_query = f"SELECT {columns_quoted_str} FROM media_items"
        
        filter_where_clauses = []
        filter_params = []
        timings['query_setup_done'] = time.perf_counter()

        if filters:
            for filter_item in filters:
                column = filter_item.get('column')
                raw_value = filter_item.get('value')
                operator = filter_item.get('operator', 'contains')

                if column == 'size': # Size column cannot be filtered via SQL
                    logging.warning(f"Ignoring filter on 'size' column: '{column}' as it's dynamically calculated.")
                    continue

                if raw_value == '' and operator in ['contains', 'starts_with', 'ends_with', 'greater_than', 'less_than']:
                    logging.warning(f"Ignoring filter condition: Column '{column}', Operator '{operator}' with empty value.")
                    continue

                if not column or column not in db_actual_columns: # Validate against actual DB columns for filtering
                    logging.warning(f"Ignoring filter condition: Invalid DB column '{column}'.")
                    continue

                if not operator or operator not in [op['value'] for op in data['operators']]:
                     logging.warning(f"Ignoring filter condition: Invalid operator '{operator}' for column '{column}'.")
                     continue
                
                # Optimize categorical filters: treat "contains" as "equals" for
                # State / Type columns to leverage indexes and avoid full scans.
                if column in ('state', 'type') and operator == 'contains':
                    operator = 'equals'

                clause_added_in_this_iteration = False
                if column == 'content_source':
                    value = raw_value
                    if operator == 'equals':
                        filter_where_clauses.append(f'"{column}" = ?')
                        filter_params.append(value)
                        clause_added_in_this_iteration = True
                    elif operator == 'not_equals':
                        if value == "None":
                            filter_where_clauses.append(f'"{column}" IS NOT NULL')
                        else:
                            filter_where_clauses.append(f'("{column}" IS NULL OR "{column}" != ?)')
                            filter_params.append(value)
                        clause_added_in_this_iteration = True
                    if clause_added_in_this_iteration: continue

                if operator == 'is_null':
                    filter_where_clauses.append(f'"{column}" IS NULL')
                    clause_added_in_this_iteration = True; continue
                elif operator == 'is_not_null':
                    filter_where_clauses.append(f'"{column}" IS NOT NULL')
                    clause_added_in_this_iteration = True; continue
                
                value = raw_value
                if value == "None":
                    if operator == 'equals':
                        filter_where_clauses.append(f'("{column}" IS NULL OR "{column}" = ? OR "{column}" = ?)')
                        filter_params.extend(['', 'None']); clause_added_in_this_iteration = True
                    elif operator == 'not_equals':
                        filter_where_clauses.append(f'("{column}" IS NOT NULL AND "{column}" != ? AND "{column}" != ?)')
                        filter_params.extend(['', 'None']); clause_added_in_this_iteration = True
                    if clause_added_in_this_iteration: continue
                elif value == '':
                    if operator == 'equals':
                        filter_where_clauses.append(f'"{column}" = ?'); filter_params.append(''); clause_added_in_this_iteration = True
                    elif operator == 'not_equals':
                         filter_where_clauses.append(f'"{column}" IS NOT ?'); filter_params.append(''); clause_added_in_this_iteration = True # Changed to IS NOT for NULL safety
                    if clause_added_in_this_iteration: continue
                elif value != '':
                    original_clause_count = len(filter_where_clauses)
                    if operator == 'contains': filter_where_clauses.append(f'"{column}" LIKE ?'); filter_params.append(f"%{value}%")
                    elif operator == 'equals': filter_where_clauses.append(f'"{column}" = ?'); filter_params.append(value)
                    elif operator == 'not_equals': filter_where_clauses.append(f'"{column}" IS NOT ?'); filter_params.append(value) # Changed to IS NOT
                    elif operator == 'starts_with': filter_where_clauses.append(f'"{column}" LIKE ?'); filter_params.append(f"{value}%")
                    elif operator == 'ends_with': filter_where_clauses.append(f'"{column}" LIKE ?'); filter_params.append(f"%{value}")
                    elif operator == 'greater_than':
                        try: filter_where_clauses.append(f'CAST("{column}" AS REAL) > ?'); filter_params.append(float(value))
                        except (ValueError, TypeError): filter_where_clauses.append(f'"{column}" > ?'); filter_params.append(value)
                    elif operator == 'less_than':
                        try: filter_where_clauses.append(f'CAST("{column}" AS REAL) < ?'); filter_params.append(float(value))
                        except (ValueError, TypeError): filter_where_clauses.append(f'"{column}" < ?'); filter_params.append(value)
                    if len(filter_where_clauses) > original_clause_count: clause_added_in_this_iteration = True; continue
        
        final_where_clause = ""
        final_params = []
        effective_content_type = content_type_req if content_type_req is not None else 'movie'
        effective_current_letter = current_letter_req if current_letter_req is not None else 'A'

        if filter_where_clauses:
            filter_combination_operator = f" {filter_logic} "
            final_where_clause = "WHERE (" + filter_combination_operator.join(filter_where_clauses) + ")"
            final_params = filter_params
            content_type_for_template = 'all' # Filters take precedence
            current_letter_for_template = ''    # Filters take precedence
        else:
            default_clauses = []
            default_params = []
            if effective_content_type != 'all':
                default_clauses.append("\"type\" = ?")
                default_params.append(effective_content_type)
            if effective_current_letter:
                if effective_current_letter == '#':
                    numeric_likes = " OR ".join([f"title LIKE '{i}%'" for i in range(10)])
                    symbol_likes = " OR ".join([f"title LIKE '{s}%'" for s in ['[', '(', '{']]) # Example symbols
                    default_clauses.append(f"({numeric_likes} OR {symbol_likes})")
                elif effective_current_letter.isalpha() and len(effective_current_letter) == 1:
                    default_clauses.append("title LIKE ?")
                    default_params.append(f"{effective_current_letter.upper()}%")
            if default_clauses:
                final_where_clause = "WHERE " + " AND ".join(default_clauses)
                final_params = default_params
            content_type_for_template = effective_content_type
            current_letter_for_template = effective_current_letter
        
        order_clause = ""
        # SQL sorting only if not sorting by 'size' and sort_column is a real DB column
        if sort_column_req != 'size' and sort_column_req in db_actual_columns:
            order_clause = f'ORDER BY "{sort_column_req}" {sort_order_req}'
        
        query = f"{base_query} {final_where_clause} {order_clause}"
        logging.debug(f"Executing query: {query} with params: {final_params}")
        timings['filter_processing_done'] = time.perf_counter()
        cursor.execute(query, final_params)
        items_from_db = cursor.fetchall()
        logging.debug(f"Fetched {len(items_from_db)} items from the database")
        timings['main_query_executed'] = time.perf_counter()
        
        items_dict_list = [dict(zip(final_columns_for_sql_query_list, item_row)) for item_row in items_from_db]

        if needs_size_data:
            for item_dict in items_dict_list:
                loc = item_dict.get('location_on_disk')
                orig_path = item_dict.get('original_path_for_symlink')
                item_dict['size_gb'] = get_item_size_gb(loc, orig_path)
        
        if sort_column_req == 'size':
            items_dict_list.sort(key=lambda x: x.get('size_gb', 0.0), reverse=(sort_order_req == 'desc'))
        timings['item_data_processing_done'] = time.perf_counter()

        logging.info("Starting to fetch distinct column values for filters.")
        time_before_all_distinct_values = time.perf_counter()
        column_values = {}
        
        for column_for_distinct_fetch in db_actual_columns: # Only fetch for actual DB columns
            loop_iteration_start_time = time.perf_counter()
            if column_for_distinct_fetch == 'content_source':
                try:
                    cursor.execute(f"SELECT DISTINCT \"{column_for_distinct_fetch}\" FROM media_items WHERE \"{column_for_distinct_fetch}\" IS NOT NULL")
                    distinct_source_ids = [row[0] for row in cursor.fetchall()]
                    column_values[column_for_distinct_fetch] = distinct_source_ids
                    logging.info(f"Fetched {len(distinct_source_ids)} distinct values for 'content_source' in {time.perf_counter() - loop_iteration_start_time:.4f}s.")
                except Exception as e:
                    logging.error(f"Error fetching distinct content_source IDs for '{column_for_distinct_fetch}': {e}")
                    column_values[column_for_distinct_fetch] = [] 
            elif column_for_distinct_fetch == 'state' or column_for_distinct_fetch == 'type':
                try:
                    cursor.execute(f"SELECT DISTINCT \"{column_for_distinct_fetch}\" FROM media_items ORDER BY \"{column_for_distinct_fetch}\"")
                    values = [row[0] if row[0] is not None else "None" for row in cursor.fetchall()]
                    column_values[column_for_distinct_fetch] = values
                    logging.info(f"Fetched {len(values)} distinct values for '{column_for_distinct_fetch}' in {time.perf_counter() - loop_iteration_start_time:.4f}s.")
                except Exception as e:
                    logging.error(f"Error fetching distinct values for '{column_for_distinct_fetch}': {e}")
                    column_values[column_for_distinct_fetch] = []
            elif column_for_distinct_fetch == 'version':
                version_fetch_start_time = time.perf_counter()
                try:
                    cursor.execute(f"SELECT DISTINCT \"{column_for_distinct_fetch}\" FROM media_items")
                    db_versions_raw = [row[0] for row in cursor.fetchall()]
                    version_list_for_dropdown = []
                    has_actual_none_or_empty = False
                    for v_name_raw in db_versions_raw:
                        if v_name_raw is None or v_name_raw == "":
                            has_actual_none_or_empty = True; continue
                        v_name = str(v_name_raw) 
                        version_list_for_dropdown.append(v_name)
                    if has_actual_none_or_empty: version_list_for_dropdown.append("None")
                    column_values[column_for_distinct_fetch] = sorted(list(set(version_list_for_dropdown)))
                    logging.info(f"Fetched and generated {len(column_values[column_for_distinct_fetch])} distinct values for 'version' from DB in {time.perf_counter() - version_fetch_start_time:.4f}s.")
                except Exception as e:
                    logging.error(f"Error fetching distinct versions from DB: {e}", exc_info=True)
                    column_values[column_for_distinct_fetch] = ["None"]
        
        logging.info(f"Finished fetching all distinct column values in {time.perf_counter() - time_before_all_distinct_values:.4f}s.")
        timings['distinct_values_fetched'] = time.perf_counter()
        
        from routes.queues_routes import consolidate_items
        unique_items, _ = consolidate_items(items_dict_list)
        timings['items_consolidated'] = time.perf_counter()

        data.update({
            'items': items_dict_list,
            'result_count': len(items_dict_list),
            'filters': filters,
            'current_letter': current_letter_for_template,
            'content_type': content_type_for_template,
            'filter_logic': filter_logic,
            'column_values': column_values,
            'unique_result_count': len(unique_items),
        })

        timings['data_updated_for_template'] = time.perf_counter()

        # Calculate and log durations
        timing_log = "Database index route timing breakdown:\n"
        last_timing = request_start_time
        for key, timestamp in timings.items():
            if key != 'overall_start':
                duration = timestamp - last_timing
                timing_log += f"  - {key}: {duration:.4f} seconds\n"
                last_timing = timestamp
        total_duration = time.perf_counter() - request_start_time
        timing_log += f"Total processing time for request: {total_duration:.4f} seconds."
        logging.info(timing_log)
        data['timings'] = {k: v - request_start_time for k, v in timings.items() if k != 'overall_start'} # Relative timings for template
        data['total_request_time'] = total_duration

        if request.args.get('ajax') == '1':
            logging.info(f"Database index route finished (AJAX). Total time: {time.perf_counter() - request_start_time:.4f} seconds.")
            return jsonify(data)
        else:
            logging.info(f"Database index route finished (HTML). Total time: {time.perf_counter() - request_start_time:.4f} seconds.")
            return render_template('database.html', **data)

    except sqlite3.Error as e:
        logging.error(f"SQLite error in database route: {str(e)}")
        error_message = f"Database error: {str(e)}"
        logging.info(f"Database index route finished with SQLite error. Total time: {time.perf_counter() - request_start_time:.4f} seconds.")
        if request.args.get('ajax') == '1':
            return jsonify({'error': error_message}), 500
        else:
            flash(error_message, "error")
            return render_template('database.html', **data)
    except Exception as e:
        logging.error(f"Unexpected error in database route: {str(e)}", exc_info=True) 
        error_message = "An unexpected error occurred. Please try again later."
        logging.info(f"Database index route finished with unexpected error. Total time: {time.perf_counter() - request_start_time:.4f} seconds.")
        if request.args.get('ajax') == '1':
            return jsonify({'error': error_message}), 500
        else:
            flash(error_message, "error")
            return render_template('database.html', **data) 
    finally:
        if conn:
            conn.close()

@database_bp.route('/bulk_queue_action', methods=['POST'])
@admin_required
def bulk_queue_action():
    action = request.form.get('action')
    selected_items = request.form.getlist('selected_items')
    from routes.program_operation_routes import get_program_runner # Existing import

    target_queue = request.form.get('target_queue') 
    
    blacklist = False
    if action == 'delete':
        blacklist_str = request.form.get('blacklist', 'false')
        blacklist = blacklist_str.lower() == 'true'

    logging.info(f"Bulk action route called. Action: '{action}', Items: {selected_items[:5]}...")

    if not action or not selected_items:
        logging.warning("Bulk action returning error: Action or selected items missing.")
        return jsonify({'success': False, 'error': 'Action and selected items are required'})

    BATCH_SIZE = 450
    total_processed = 0
    error_count = 0
    errors = []

    from database import get_db_connection
    
    program_runner = get_program_runner()
    bulk_action_paused_queue = False # Flag to track if this function paused the queue

    try:
        if program_runner and program_runner.is_running() and hasattr(program_runner, 'pause_queue') and callable(program_runner.pause_queue) and hasattr(program_runner, 'resume_queue') and callable(program_runner.resume_queue):
            logging.info("Attempting to pause program queue for bulk DB action.")
            # Set the pause reason specifically for this bulk action
            program_runner.pause_info = { # Assuming pause_info attribute exists and is used by pause_queue
                "reason_string": "Bulk database operation in progress",
                "error_type": "SYSTEM_MAINTENANCE", 
                "service_name": "Database Bulk Action",
                "status_code": None,
                "retry_count": 0
            }
            program_runner.pause_queue() 
            bulk_action_paused_queue = True
            logging.info("Program queue paused successfully for bulk action.")
        else:
            log_message = "Program runner not found, not running, or missing pause_queue/resume_queue methods. Proceeding without pausing queue."
            if program_runner:
                if not program_runner.is_running():
                    log_message = "Program runner found but not running. Proceeding without pausing queue."
                elif not (hasattr(program_runner, 'pause_queue') and callable(program_runner.pause_queue)):
                    log_message = "Program runner found and running, but 'pause_queue' method is missing. Proceeding without pausing queue."
                elif not (hasattr(program_runner, 'resume_queue') and callable(program_runner.resume_queue)):
                    log_message = "Program runner found and running, but 'resume_queue' method is missing. Proceeding without pausing queue."
            logging.info(log_message)


        for i in range(0, len(selected_items), BATCH_SIZE):
            batch = selected_items[i:i + BATCH_SIZE]
            logging.info(f"Processing batch {i//BATCH_SIZE + 1}. Action: '{action}'")

            if action == 'delete':
                logging.info("Entering 'delete' block.")
                # Process each item in the batch through delete_item
                for item_id in batch:
                    try:
                        # Create a new request with our data
                        with current_app.test_request_context(
                            method='POST',
                            data=json.dumps({
                                'item_id': item_id,
                                'blacklist': blacklist
                            }),
                            content_type='application/json'
                        ):
                            response = delete_item()
                            
                            if isinstance(response, tuple):
                                success = response[0].json.get('success', False)
                                if response[0].json.get('error') == 'database is locked':
                                    # Propagate the specific error response
                                    return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
                            else:
                                success = response.json.get('success', False)
                                if response.json.get('error') == 'database is locked':
                                     return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
                                
                            if success:
                                total_processed += 1
                            else:
                                error_count += 1
                                error_msg = response.json.get('error', 'Unknown error')
                                errors.append(f"Error processing item {item_id}: {error_msg}")
                                
                    except sqlite3.OperationalError as e:
                        if "database is locked" in str(e):
                            logging.error(f"Database is locked during bulk delete for item {item_id}.")
                            return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
                        else:
                            error_count += 1
                            errors.append(f"Error processing item {item_id}: {str(e)}")
                            logging.error(f"Error processing item {item_id} in bulk delete: {str(e)}")
                    except Exception as e:
                        error_count += 1
                        errors.append(f"Error processing item {item_id}: {str(e)}")
                        logging.error(f"Error processing item {item_id} in bulk delete: {str(e)}")
                        
            elif action == 'move' and target_queue:
                logging.info("Entering 'move' block.")
                # Keep existing move functionality
                conn = get_db_connection()
                try:
                    cursor = conn.cursor()
                    placeholders = ','.join('?' * len(batch))
                    cursor.execute(
                        f'UPDATE media_items SET state = ?, last_updated = ? WHERE id IN ({placeholders})',
                        [target_queue, datetime.now()] + batch
                    )
                    total_processed += cursor.rowcount
                    conn.commit()
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e):
                        logging.error("Database is locked during bulk move.")
                        conn.rollback()
                        return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
                    else:
                        error_count += 1
                        conn.rollback()
                        errors.append(f"Error in batch {i//BATCH_SIZE + 1}: {str(e)}")
                        logging.error(f"Error in batch {i//BATCH_SIZE + 1}: {str(e)}")
                except Exception as e:
                    error_count += 1
                    conn.rollback()
                    errors.append(f"Error in batch {i//BATCH_SIZE + 1}: {str(e)}")
                    logging.error(f"Error in batch {i//BATCH_SIZE + 1}: {str(e)}")
                finally:
                    conn.close()
            elif action == 'change_version' and target_queue:  # target_queue contains the version in this case
                logging.info("Entering 'change_version' block.")
                conn = get_db_connection()
                try:
                    cursor = conn.cursor()
                    placeholders = ','.join('?' * len(batch))
                    cursor.execute(
                        f'UPDATE media_items SET version = ?, last_updated = ? WHERE id IN ({placeholders})',
                        [target_queue, datetime.now()] + batch
                    )
                    total_processed += cursor.rowcount
                    conn.commit()
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e):
                        logging.error("Database is locked during bulk change_version.")
                        conn.rollback()
                        return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
                    else:
                        error_count += 1
                        conn.rollback()
                        errors.append(f"Error in batch {i//BATCH_SIZE + 1}: {str(e)}")
                        logging.error(f"Error in batch {i//BATCH_SIZE + 1}: {str(e)}")
                except Exception as e:
                    error_count += 1
                    conn.rollback()
                    errors.append(f"Error in batch {i//BATCH_SIZE + 1}: {str(e)}")
                    logging.error(f"Error in batch {i//BATCH_SIZE + 1}: {str(e)}")
                finally:
                    conn.close()
            elif action == 'early_release':
                logging.info("Entering 'early_release' block.")
                # Handle early release action
                conn = get_db_connection()
                try:
                    cursor = conn.cursor()
                    placeholders = ','.join('?' * len(batch))
                    cursor.execute(
                        f'UPDATE media_items SET early_release = TRUE, state = ?, last_updated = ? WHERE id IN ({placeholders})',
                        ['Wanted', datetime.now()] + batch
                    )
                    total_processed += cursor.rowcount
                    conn.commit()
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e):
                        logging.error("Database is locked during bulk early_release.")
                        conn.rollback()
                        return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
                    else:
                        error_count += 1
                        conn.rollback()
                        errors.append(f"Error in batch {i//BATCH_SIZE + 1}: {str(e)}")
                        logging.error(f"Error in batch {i//BATCH_SIZE + 1}: {str(e)}")
                except Exception as e:
                    error_count += 1
                    conn.rollback()
                    errors.append(f"Error in batch {i//BATCH_SIZE + 1}: {str(e)}")
                    logging.error(f"Error in batch {i//BATCH_SIZE + 1}: {str(e)}")
                finally:
                    conn.close()
            elif action == 'rescrape':
                logging.info(f"Entering 'rescrape' block for batch: {batch}") # batch is a list of item IDs for this BATCH_SIZE chunk
                # Get file management settings (once per BATCH_SIZE chunk)
                file_management = get_setting('File Management', 'file_collection_management', 'Plex')
                mounted_location = get_setting('Plex', 'mounted_file_location', get_setting('File Management', 'original_files_path', ''))
                original_files_path = get_setting('File Management', 'original_files_path', '')
                symlinked_files_path = get_setting('File Management', 'symlinked_files_path', '')

                items_in_batch_details_raw = [] # To store raw data fetched from DB for this batch of IDs
                
                conn_rescape_batch = None 
                try:
                    from database import get_db_connection 
                    conn_rescape_batch = get_db_connection()
                    cursor_rescape_batch = conn_rescape_batch.cursor()

                    placeholders_select = ','.join('?' * len(batch)) # 'batch' here is the current chunk of item IDs
                    query_select = f"""
                        SELECT id, state, location_on_disk, original_path_for_symlink, 
                               filled_by_file, title, type, episode_title, version, original_scraped_torrent_title
                        FROM media_items 
                        WHERE id IN ({placeholders_select})
                    """
                    cursor_rescape_batch.execute(query_select, batch)
                    db_columns = [column[0] for column in cursor_rescape_batch.description]
                    items_in_batch_details_raw = [dict(zip(db_columns, row)) for row in cursor_rescape_batch.fetchall()]
                
                except Exception as e:
                    logging.error(f"Error fetching batch details for rescrape: {str(e)}", exc_info=True)
                    errors.append(f"Error fetching details for batch {i//BATCH_SIZE + 1}: {str(e)}")
                    if conn_rescape_batch: conn_rescape_batch.close()
                    continue # Skip to the next BATCH_SIZE chunk of selected_items
                
                prepared_items_for_db_update = [] 

                for item_db_data in items_in_batch_details_raw: 
                    item_id = item_db_data['id']
                    try:
                        logging.info(f"Rescrape: Processing item_id: {item_id} for file/Plex ops. Current state: {item_db_data.get('state')}, Version: {item_db_data.get('version')}")

                        # --- Start: File Deletion & Plex Removal Logic (using item_db_data) ---
                        if item_db_data['state'] in ['Collected', 'Upgrading']:
                            if file_management == 'Plex' and item_db_data.get('filled_by_file'):
                                if item_db_data['type'] == 'movie':
                                    cache_plex_removal(item_db_data['title'], item_db_data['filled_by_file'])
                                elif item_db_data['type'] == 'episode':
                                    cache_plex_removal(item_db_data['title'], item_db_data['filled_by_file'], item_db_data.get('episode_title'))
                                logging.info(f"Rescrape: Queued Plex removal for item {item_id} (Plex mode).")
                            elif file_management == 'Symlinked/Local' and item_db_data.get('location_on_disk'): # Check location_on_disk for path
                                # Path for symlinked items should be location_on_disk, which is the symlink path
                                path_to_remove = item_db_data['location_on_disk']
                                if item_db_data['type'] == 'movie':
                                    cache_plex_removal(item_db_data['title'], path_to_remove)
                                elif item_db_data['type'] == 'episode':
                                    cache_plex_removal(item_db_data['title'], path_to_remove, item_db_data.get('episode_title'))
                                logging.info(f"Rescrape: Queued Plex removal for item {item_id} (Symlinked/Local mode with Plex URL). Path: {path_to_remove}")
                        
                        if item_db_data['state'] in ['Collected', 'Upgrading'] and \
                           (item_db_data.get('location_on_disk') or item_db_data.get('original_path_for_symlink')):
                            sleep(0.5) 

                        if item_db_data['state'] in ['Collected', 'Upgrading']:
                            if file_management == 'Plex' and item_db_data.get('filled_by_file'):
                                if item_db_data['type'] == 'movie':
                                    cache_plex_removal(item_db_data['title'], item_db_data['filled_by_file'])
                                elif item_db_data['type'] == 'episode':
                                    cache_plex_removal(item_db_data['title'], item_db_data['filled_by_file'], item_db_data.get('episode_title'))
                                logging.info(f"Rescrape: Queued Plex removal for item {item_id} (Plex mode).")
                            elif file_management == 'Symlinked/Local' and item_db_data.get('location_on_disk'): # Check location_on_disk for path
                                # Path for symlinked items should be location_on_disk, which is the symlink path
                                path_to_remove = item_db_data['location_on_disk']
                                if item_db_data['type'] == 'movie':
                                    cache_plex_removal(item_db_data['title'], path_to_remove)
                                elif item_db_data['type'] == 'episode':
                                    cache_plex_removal(item_db_data['title'], path_to_remove, item_db_data.get('episode_title'))
                                logging.info(f"Rescrape: Queued Plex removal for item {item_id} (Symlinked/Local mode with Plex URL). Path: {path_to_remove}")
                        # --- End: File Deletion & Plex Removal Logic ---

                        current_version_val = item_db_data.get('version')

                        cleaned_version_val = current_version_val # Default assignment

                        if current_version_val is None:
                            logging.warning(f"Rescrape Detail: Item ID {item_id} - Version from DB is None. 'cleaned_version_val' will be None.")
                            # cleaned_version_val is already None
                        elif isinstance(current_version_val, str):
                            if '*' in current_version_val:
                                cleaned_version_val = current_version_val.replace('*', '')
                        else: # Not a string and not None
                            logging.warning(f"Rescrape Detail: Item ID {item_id} - Version from DB is not a string or None: '{current_version_val}' (type: {type(current_version_val)}). 'cleaned_version_val' currently is '{cleaned_version_val}'. This might cause issues if DB expects a string for version.")
                            # cleaned_version_val will hold the original non-string, non-None value here.

                        prepared_items_for_db_update.append({
                            'id': item_id,
                            'cleaned_version': cleaned_version_val,
                            'current_original_scraped_title': item_db_data.get('original_scraped_torrent_title') # Store for rescrape_original_torrent_title
                        })

                    except Exception as e_indiv_item_proc: 
                        error_count += 1
                        error_msg = f"Error during file/Plex processing for item {item_id} (for rescrape): {str(e_indiv_item_proc)}"
                        errors.append(error_msg)
                        logging.error(f"Rescrape: {error_msg}", exc_info=True)
                
                if prepared_items_for_db_update: 
                    try:
                        item_ids_for_update_clause = [item['id'] for item in prepared_items_for_db_update]
                        placeholders_for_in_clause = ','.join('?' * len(item_ids_for_update_clause))

                        version_case_sql_parts = []
                        params_for_version_case_values = []
                        for item_update_payload in prepared_items_for_db_update:
                            version_case_sql_parts.append("WHEN ? THEN ?")
                            params_for_version_case_values.extend([item_update_payload['id'], item_update_payload['cleaned_version']])

                        version_case_final_sql = "version"
                        if version_case_sql_parts:
                             version_case_final_sql = "CASE id " + " ".join(version_case_sql_parts) + " ELSE version END"

                        rescrape_title_case_sql_parts = []
                        params_for_rescrape_title_case_values = []
                        for item_update_payload in prepared_items_for_db_update:
                            rescrape_title_case_sql_parts.append("WHEN ? THEN ?")
                            params_for_rescrape_title_case_values.extend([item_update_payload['id'], item_update_payload.get('current_original_scraped_title')])

                        rescrape_title_case_final_sql = "rescrape_original_torrent_title" # Default to existing if no specific update
                        if rescrape_title_case_sql_parts:
                            rescrape_title_case_final_sql = "CASE id " + " ".join(rescrape_title_case_sql_parts) + " ELSE rescrape_original_torrent_title END"

                        # MOVED DEFINITIONS UP
                        final_db_update_query = f"""UPDATE media_items 
                               SET state = 'Wanted', 
                                   location_on_disk = NULL, 
                                   original_path_for_symlink = NULL, 
                                   filled_by_file = NULL,
                                   filled_by_title = NULL,
                                   filled_by_magnet = NULL,
                                   filled_by_torrent_id = NULL,
                                   collected_at = NULL,
                                   rescrape_original_torrent_title = {rescrape_title_case_final_sql},
                                   original_scraped_torrent_title = NULL,
                                   upgrading_from = NULL,
                                   upgrading = NULL,
                                   version = {version_case_final_sql},
                                   fall_back_to_single_scraper = 0,
                                   last_updated = ? 
                               WHERE id IN ({placeholders_for_in_clause})"""

                        sql_params_for_final_db_update = params_for_rescrape_title_case_values + params_for_version_case_values + [datetime.now()] + item_ids_for_update_clause
                        
                        cursor_rescape_batch.execute(final_db_update_query, sql_params_for_final_db_update)
                        rows_affected_by_update = cursor_rescape_batch.rowcount

                        if rows_affected_by_update == len(item_ids_for_update_clause):
                            conn_rescape_batch.commit()
                            total_processed += rows_affected_by_update
                            logging.info(f"Rescrape: Successfully committed DB update for {rows_affected_by_update} items for batch {i//BATCH_SIZE + 1}.")
                        else:
                            conn_rescape_batch.rollback()
                            mismatch_error_msg = f"Rescrape DB Update: Expected to affect {len(item_ids_for_update_clause)} items, but DB reported {rows_affected_by_update}. Rolled back changes for this group of items in batch {i//BATCH_SIZE + 1}."
                            logging.error(mismatch_error_msg)
                            errors.append(mismatch_error_msg)
                            error_count += len(item_ids_for_update_clause) 
                    
                    except sqlite3.OperationalError as e_db_update:
                        if "database is locked" in str(e_db_update):
                            logging.error(f"Database is locked during bulk rescrape update for batch {i//BATCH_SIZE + 1}.")
                            if conn_rescape_batch: conn_rescape_batch.rollback()
                            # Specific error response for database locked
                            return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
                        else:
                            if conn_rescape_batch: 
                                try:
                                    conn_rescape_batch.rollback() 
                                except Exception as e_rollback:
                                    logging.error(f"Rescrape: Error during rollback attempt: {e_rollback}", exc_info=True)

                            db_update_err_msg = f"Error during batch database update for rescrape (batch {i//BATCH_SIZE + 1}): {str(e_db_update)}"
                            errors.append(db_update_err_msg)
                            logging.error(f"Rescrape: {db_update_err_msg}", exc_info=True)
                            error_count += len(prepared_items_for_db_update)
                    except Exception as e_db_update: 
                        if conn_rescape_batch: 
                            try:
                                conn_rescape_batch.rollback() 
                            except Exception as e_rollback:
                                logging.error(f"Rescrape: Error during rollback attempt: {e_rollback}", exc_info=True)

                        db_update_err_msg = f"Error during batch database update for rescrape (batch {i//BATCH_SIZE + 1}): {str(e_db_update)}"
                        errors.append(db_update_err_msg)
                        logging.error(f"Rescrape: {db_update_err_msg}", exc_info=True)
                        error_count += len(prepared_items_for_db_update) 
                
                elif items_in_batch_details_raw: 
                    logging.info(f"Rescrape: No items from batch {i//BATCH_SIZE + 1} were successfully prepared for database update (e.g., all had file/Plex processing errors).")

                if conn_rescape_batch:
                    conn_rescape_batch.close()
                # --- End New Rescrape Logic ---
            elif action == 'force_priority':
                logging.info("Entering 'force_priority' block.")
                conn = get_db_connection()
                try:
                    cursor = conn.cursor()
                    placeholders = ','.join('?' * len(batch))
                    cursor.execute(
                        f'UPDATE media_items SET force_priority = TRUE, last_updated = ? WHERE id IN ({placeholders})',
                        [datetime.now()] + batch
                    )
                    total_processed += cursor.rowcount
                    conn.commit()
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e):
                        logging.error("Database is locked during bulk force_priority.")
                        conn.rollback()
                        return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
                    else:
                        error_count += 1
                        conn.rollback()
                        errors.append(f"Error in batch {i//BATCH_SIZE + 1}: {str(e)}")
                        logging.error(f"Error in batch {i//BATCH_SIZE + 1}: {str(e)}")
                except Exception as e:
                    error_count += 1
                    conn.rollback()
                    errors.append(f"Error in batch {i//BATCH_SIZE + 1}: {str(e)}")
                    logging.error(f"Error in batch {i//BATCH_SIZE + 1}: {str(e)}")
                finally:
                    conn.close()
            elif action == 'resync':
                logging.info("Entering 'resync' block.")
                for item_id in batch:
                    try:
                        item = get_media_item_by_id(item_id)
                        if not item:
                            error_count += 1
                            errors.append(f"Item {item_id} not found")
                            continue

                        old_symlink_path = item.get('location_on_disk')
                        # Determine source file path
                        source_file_path = None
                        if item.get('original_path_for_symlink') and os.path.exists(item['original_path_for_symlink']):
                            source_file_path = item['original_path_for_symlink']
                        elif old_symlink_path and os.path.islink(old_symlink_path):
                            source_file_path = os.path.realpath(old_symlink_path)
                        else:
                            # Fallback – use whatever is stored if it exists
                            source_file_path = old_symlink_path if old_symlink_path and os.path.exists(old_symlink_path) else None

                        if not source_file_path or not os.path.exists(source_file_path):
                            error_count += 1
                            errors.append(f"Item {item_id}: source file not found for resync")
                            continue

                        # Prepare a copy for convert_item_to_symlink with correct source
                        item_copy = item.copy()
                        item_copy['location_on_disk'] = source_file_path

                        result = convert_item_to_symlink(item_copy, skip_verification=True)
                        if result.get('success'):
                            # Remove old symlink only if the path has changed
                            if old_symlink_path and result['new_location'] and \
                               os.path.normpath(old_symlink_path) != os.path.normpath(result['new_location']) and \
                               os.path.islink(old_symlink_path):
                                try:
                                    os.unlink(old_symlink_path)
                                except Exception as unlink_err:
                                    logging.warning(f"Failed to remove old symlink for item {item_id}: {unlink_err}")
                            # Update DB to new paths
                            update_media_item(
                                item_id,
                                location_on_disk=result['new_location'],
                                original_path_for_symlink=source_file_path
                            )
                            total_processed += 1
                        else:
                            error_count += 1
                            errors.append(f"Item {item_id}: {result.get('error')}")
                    except sqlite3.OperationalError as e:
                        if "database is locked" in str(e):
                            logging.error("Database is locked during bulk resync.")
                            return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
                        error_count += 1
                        errors.append(f"Item {item_id}: {str(e)}")
                    except Exception as e:
                        error_count += 1
                        errors.append(f"Item {item_id}: {str(e)}")
            else:
                logging.warning(f"Bulk action returning error: Invalid action '{action}'")
                # No need to explicitly resume here, finally block will handle it.
                return jsonify({'success': False, 'error': 'Invalid action or missing target queue'})

        if error_count > 0:
            message = f"Completed with {error_count} errors. Successfully processed {total_processed} items."
            if errors:
                message += f" First few errors: {'; '.join(errors[:3])}"
            return jsonify({'success': True, 'message': message, 'warning': True})
        else:
            action_map = {
                "delete": "deleted",
                "move": f"moved to {target_queue} queue",
                "change_version": f"changed to version {target_queue}",
                "early_release": "marked as early release and moved to Wanted queue",
                "rescrape": "deleted files/Plex entries for and moved to Wanted queue", # Added rescrape message
                "force_priority": "marked for forced priority",
                "resync": "resynchronized"
            }
            action_text = action_map.get(action, f"processed ({action})")
            message = f"Successfully {action_text} {total_processed} items"
            return jsonify({'success': True, 'message': message})

    except sqlite3.OperationalError as e:
        if "database is locked" in str(e):
            logging.error(f"Database is locked during outer try block for bulk action '{action}'.")
            return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
        else:
            logging.error(f"Outer operational error in bulk action '{action}': {str(e)}", exc_info=True)
            return jsonify({'success': False, 'error': f"An operational error occurred during bulk {action}: {str(e)}"}), 500
    except Exception as e:
        logging.error(f"Outer exception in bulk action '{action}': {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': f"An unexpected error occurred during bulk {action}: {str(e)}"}), 500
    finally:
        if bulk_action_paused_queue and program_runner and hasattr(program_runner, 'resume_queue') and callable(program_runner.resume_queue):
            logging.info("Resuming program queue in finally block after bulk DB action.")
            program_runner.resume_queue() 
        elif program_runner and not bulk_action_paused_queue:
            logging.info("Queue was not paused by this bulk operation or program_runner not available/suitable for resume.")

@database_bp.route('/delete_item', methods=['POST'])
@admin_required
def delete_item():
    data = request.get_json()
    item_id = data.get('item_id')
    blacklist = data.get('blacklist', False)
    
    if not item_id:
        return jsonify({'success': False, 'error': 'No item ID provided'}), 400

    try:
        item = get_media_item_by_id(item_id)
        if not item:
            return jsonify({'success': False, 'error': 'Item not found'}), 404

        # Get file management settings
        file_management = get_setting('File Management', 'file_collection_management', 'Plex')
        mounted_location = get_setting('Plex', 'mounted_file_location', get_setting('File Management', 'original_files_path', ''))
        original_files_path = get_setting('File Management', 'original_files_path', '')
        symlinked_files_path = get_setting('File Management', 'symlinked_files_path', '')

        if item['state'] == 'Collected' or item['state'] == 'Upgrading':
            if file_management == 'Plex':
                if mounted_location and item.get('location_on_disk'):
                    try:
                        if os.path.exists(item['location_on_disk']):
                            os.remove(item['location_on_disk'])
                            logging.info(f"Delete item: Removed file from disk {item['location_on_disk']} (Plex mode).")
                    except Exception as e:
                        logging.error(f"Error deleting file at {item['location_on_disk']}: {str(e)}")

                # Allow time for file system operations to complete
                sleep(1)

                # Immediate Plex removal
                path_to_remove_from_plex = item.get('filled_by_file')
                if path_to_remove_from_plex:
                    try:
                        logging.info(f"Delete item: Attempting immediate Plex removal for {item['title']} ({path_to_remove_from_plex}).")
                        remove_file_from_plex(item['title'], path_to_remove_from_plex, item.get('episode_title'))
                        logging.info(f"Delete item: Successfully processed immediate Plex removal for {item['title']} ({path_to_remove_from_plex}).")
                    except Exception as e:
                        logging.error(f"Delete item: Error during immediate Plex removal for {item['title']} ({path_to_remove_from_plex}): {str(e)}.")
                else:
                    logging.warning(f"Delete item: No 'filled_by_file' path for item {item_id} ({item['title']}). Skipping Plex removal.")

            elif file_management == 'Symlinked/Local':
                symlink_path_to_remove_disk = item.get('location_on_disk')
                original_file_path_to_remove_disk = item.get('original_path_for_symlink')
                
                # Determine the path Plex uses, prioritizing the symlink path
                path_for_plex_api_call = None
                if symlink_path_to_remove_disk:
                    path_for_plex_api_call = symlink_path_to_remove_disk
                    try:
                        if os.path.exists(symlink_path_to_remove_disk) and os.path.islink(symlink_path_to_remove_disk):
                            os.unlink(symlink_path_to_remove_disk)
                            logging.info(f"Delete item: Removed symlink {symlink_path_to_remove_disk} (Symlinked/Local mode).")
                    except Exception as e:
                        logging.error(f"Error removing symlink at {symlink_path_to_remove_disk}: {str(e)}")
                
                if original_file_path_to_remove_disk:
                    if not path_for_plex_api_call: # Fallback if symlink path wasn't set
                        path_for_plex_api_call = original_file_path_to_remove_disk
                    try:
                        if os.path.exists(original_file_path_to_remove_disk):
                            os.remove(original_file_path_to_remove_disk)
                            logging.info(f"Delete item: Removed original file {original_file_path_to_remove_disk} (Symlinked/Local mode).")
                    except Exception as e:
                        logging.error(f"Error deleting original file at {original_file_path_to_remove_disk}: {str(e)}")

                # Allow time for file system operations to complete
                sleep(1)

                # Immediate Plex removal using the determined path
                if path_for_plex_api_call:
                    try:
                        logging.info(f"Delete item: Attempting immediate Plex removal for {item['title']} using path {path_for_plex_api_call} (Symlinked/Local mode).")
                        remove_file_from_plex(item['title'], path_for_plex_api_call, item.get('episode_title'))
                        logging.info(f"Delete item: Successfully processed immediate Plex removal for {item['title']} ({path_for_plex_api_call}).")
                    except Exception as e:
                        logging.error(f"Delete item: Error during immediate Plex removal for {item['title']} ({path_for_plex_api_call}): {str(e)}.")
                else:
                    logging.warning(f"Delete item: No suitable path found for Plex removal for item {item_id} ({item['title']}) (Symlinked/Local mode). Skipping Plex removal.")

        # Handle database operation based on blacklist flag
        if blacklist:
            from database import update_media_item_state
            update_media_item_state(item_id, 'Blacklisted')
        else:
            from database import remove_from_media_items
            remove_from_media_items(item_id)

        return jsonify({'success': True})
    except sqlite3.OperationalError as e:
        if "database is locked" in str(e):
            logging.error(f"Database is locked during delete_item for item_id {item_id}.")
            return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
        else:
            logging.error(f"Operational error processing delete request for item_id {item_id}: {str(e)}")
            return jsonify({'success': False, 'error': str(e)}), 500
    except Exception as e:
        logging.error(f"Error processing delete request: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

def perform_database_migration():
    # logging.info("Performing database migration...")
    inspector = inspect(db.engine)
    if not inspector.has_table("user"):
        # If the user table doesn't exist, create all tables
        db.create_all()
    else:
        # Check if onboarding_complete column exists
        columns = [c['name'] for c in inspector.get_columns('user')]
        if 'onboarding_complete' not in columns:
            # Add onboarding_complete column
            with db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE user ADD COLUMN onboarding_complete BOOLEAN DEFAULT FALSE"))
                conn.commit()
    
    # Commit the changes
    db.session.commit()

@database_bp.route('/reverse_parser', methods=['GET', 'POST'])
@admin_required
def reverse_parser():
    # config = load_config() # Not strictly needed here anymore for version_settings
    # version_settings = config.get('Scraping', {}).get('versions', {}) # Unused in this route directly
    logging.debug("Entering reverse_parser function")
    data = {
        'selected_columns': ['title', 'filled_by_file', 'version'],
        'sort_column': 'title',
        'sort_order': 'asc'
    }
    try:
        from database import get_db_connection
        conn = get_db_connection()
        cursor = conn.cursor()

        page = int(request.args.get('page', 1))
        items_per_page = 100
        filter_default = request.args.get('filter_default', 'false').lower() == 'true'

        logging.debug(f"page: {page}, items_per_page: {items_per_page}, filter_default: {filter_default}")

        # Fetch the latest settings every time
        version_terms = get_version_settings()
        default_version = get_default_version()
        version_order = get_version_order()

        # Construct the base query
        query = f"""
            SELECT id, {', '.join(data['selected_columns'])}
            FROM media_items
            WHERE state = 'Collected'
        """
        
        params = []

        # Add filtering logic
        if filter_default:
            version_conditions = []
            for version, terms in version_terms.items():
                if terms:
                    term_conditions = " OR ".join(["filled_by_file LIKE ?" for _ in terms])
                    version_conditions.append(f"({term_conditions})")
                    params.extend([f"%{term}%" for term in terms])
            
            if version_conditions:
                query += f" AND NOT ({' OR '.join(version_conditions)})"

        # Add sorting and pagination
        query += f" ORDER BY {data['sort_column']} {data['sort_order']}"
        query += f" LIMIT {items_per_page} OFFSET {(page - 1) * items_per_page}"

        logging.debug(f"Executing query: {query}")
        logging.debug(f"Query parameters: {params}")

        cursor.execute(query, params)
        items = cursor.fetchall()

        logging.debug(f"Fetched {len(items)} items from the database")

        conn.close()

        items = [dict(zip(['id'] + data['selected_columns'], item)) for item in items]

        # Parse versions using parse_filename_for_version function
        for item in items:
            parsed_version = parse_filename_for_version(item['filled_by_file'])
            item['parsed_version'] = parsed_version
            logging.debug(f"Filename: {item['filled_by_file']}, Parsed Version: {parsed_version}")

        data.update({
            'items': items,
            'page': page,
            'filter_default': filter_default,
            'default_version': default_version,
            'version_terms': version_terms,
            'version_order': version_order
        })

        if request.args.get('ajax') == '1':
            return jsonify(data)
        else:
            return render_template('reverse_parser.html', **data)
        
    except sqlite3.Error as e:
        logging.error(f"SQLite error in reverse_parser route: {str(e)}")
        error_message = f"Database error: {str(e)}"
    except Exception as e:
        logging.error(f"Unexpected error in reverse_parser route: {str(e)}")
        error_message = "An unexpected error occurred. Please try again later."

    if request.args.get('ajax') == '1':
        return jsonify({'error': error_message}), 500
    else:
        flash(error_message, "error")
        return render_template('reverse_parser.html', **data)
    
@database_bp.route('/apply_parsed_versions', methods=['POST'])
@admin_required
def apply_parsed_versions():
    data = request.get_json()
    items_to_update = data.get('items_to_update', [])
    updated_count = 0
    errors = []
    database_locked_encountered = False

    for item in items_to_update:
        if item['filled_by_file']:
            parsed_version = parse_filename_for_version(item['filled_by_file'])
            
            current_version = item.get('version') # Use .get() for safety
            if parsed_version != current_version:
                try:
                    from database import update_media_item_state # Assuming this handles its own DB connection
                    update_media_item_state(item['id'], item['state'], version=parsed_version)
                    updated_count += 1
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e):
                        logging.error(f"Database is locked while updating item {item['id']} to version {parsed_version}.")
                        errors.append(f"Database locked for item {item['id']}.")
                        database_locked_encountered = True 
                        # Optionally break or continue, for now, we'll try others but report lock
                    else:
                        logging.error(f"Operational error updating item {item['id']}: {str(e)}")
                        errors.append(f"Error for item {item['id']}: {str(e)}")
                except Exception as e:
                    logging.error(f"Error updating item {item['id']}: {str(e)}")
                    errors.append(f"Error for item {item['id']}: {str(e)}")
    
    if database_locked_encountered:
        return jsonify({
            'success': False, 
            'error': 'database is locked', 
            'database_locked': True,
            'message': f'Database was locked. Updated {updated_count} items before encountering lock. Errors: {"; ".join(errors)}'
        }), 503

    if errors:
        return jsonify({
            'success': True, # Partial success
            'message': f'Parsed versions applied with some errors. Updated {updated_count} items. Errors: {"; ".join(errors)}',
            'warning': True
        })
    
    return jsonify({
        'success': True, 
        'message': f'Parsed versions applied successfully. Updated {updated_count} items.'
    })

@database_bp.route('/watch_history', methods=['GET'])
@admin_required
def watch_history():
    try:
        # Get database connection
        db_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        db_path = os.path.join(db_dir, 'watch_history.db')
        
        if not os.path.exists(db_path):
            flash("Watch history database not found. Please sync Plex watch history first.", "warning")
            return render_template('watch_history.html', items=[])
            
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Get filter parameters
        content_type = request.args.get('type', 'all')  # 'movie', 'episode', or 'all'
        sort_by = request.args.get('sort', 'watched_at')  # 'title' or 'watched_at'
        sort_order = request.args.get('order', 'desc')  # 'asc' or 'desc'
        
        # Build query
        query = """
            SELECT title, type, watched_at, season, episode, show_title, source
            FROM watch_history
            WHERE 1=1
        """
        params = []
        
        if content_type != 'all':
            query += " AND type = ?"
            params.append(content_type)
            
        query += f" ORDER BY {sort_by} {sort_order}"
        
        # Execute query
        cursor.execute(query, params)
        items = cursor.fetchall()
        
        # Convert to list of dicts for easier template handling
        formatted_items = []
        for item in items:
            title, type_, watched_at, season, episode, show_title, source = item
            
            # Format the watched_at date
            try:
                watched_at = datetime.strptime(watched_at, '%Y-%m-%d %H:%M:%S').strftime('%Y-%m-%d %H:%M')
            except:
                watched_at = 'Unknown'
                
            # Format the display title
            if type_ == 'episode' and show_title:
                display_title = f"{show_title} - S{season:02d}E{episode:02d} - {title}"
            else:
                display_title = title
                
            formatted_items.append({
                'title': display_title,
                'type': type_,
                'watched_at': watched_at,
                'source': source
            })
        
        conn.close()
        
        return render_template('watch_history.html',
                             items=formatted_items,
                             content_type=content_type,
                             sort_by=sort_by,
                             sort_order=sort_order)
                             
    except Exception as e:
        logging.error(f"Error in watch history route: {str(e)}")
        flash(f"Error retrieving watch history: {str(e)}", "error")
        return render_template('watch_history.html', items=[])

@database_bp.route('/watch_history/clear', methods=['POST'])
@admin_required
def clear_watch_history():
    try:
        # Get database connection
        db_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        db_path = os.path.join(db_dir, 'watch_history.db')
        
        if not os.path.exists(db_path):
            return jsonify({'success': False, 'error': 'Watch history database not found'})
            
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Clear the watch history table
        cursor.execute('DELETE FROM watch_history')
        
        # Reset the auto-increment counter
        cursor.execute('DELETE FROM sqlite_sequence WHERE name = "watch_history"')
        
        conn.commit()
        conn.close()
        
        logging.info("Watch history cleared successfully")
        return jsonify({'success': True})
    except sqlite3.OperationalError as e:
        if "database is locked" in str(e):
            logging.error("Database is locked during clear_watch_history.")
            # conn might not be defined or closed if error happened early in connect
            try:
                if conn: conn.rollback() # Rollback if possible
            except: pass 
            return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
        else:
            logging.error(f"Operational error clearing watch history: {str(e)}")
            try:
                if conn: conn.rollback()
            except: pass
            return jsonify({'success': False, 'error': str(e)}), 500
    except Exception as e:
        logging.error(f"Error clearing watch history: {str(e)}")
        try:
            if conn: conn.rollback()
        except: pass
        return jsonify({'success': False, 'error': str(e)}), 500

@database_bp.route('/phalanxdb')
@admin_required
def phalanxdb_status():
    """Display the PhalanxDB status and contents"""
    try:
        # Check if service is enabled
        enabled = get_setting('UI Settings', 'enable_phalanx_db', False)
        
        if not enabled:
            return render_template(
                'phalanxdb_status.html',
                connection_status=False,
                mesh_status={
                    'syncsSent': 0,
                    'syncsReceived': 0,
                    'lastSyncAt': datetime.now().isoformat(),
                    'connectionsActive': 0,
                    'databaseEntries': 0,
                    'nodeId': 'unavailable',
                    'memory': {
                        'heapTotal': '0 MB',
                        'heapUsed': '0 MB',
                        'rss': '0 MB',
                        'external': '0 MB'
                    }
                },
                enabled=False
            )

        # Initialize cache manager
        phalanx_manager = PhalanxDBClassManager()
        
        # Get connection and mesh status
        connection_status = phalanx_manager.test_connection()
        mesh_status = phalanx_manager.get_mesh_status()
        
        return render_template(
            'phalanxdb_status.html',
            connection_status=connection_status,
            mesh_status=mesh_status,
            enabled=True
        )
        
    except Exception as e:
        logging.error(f"Error in PhalanxDB status route: {str(e)}")
        flash(f"Error retrieving PhalanxDB status: {str(e)}", "error")
        return render_template(
            'phalanxdb_status.html',
            connection_status=False,
            mesh_status={
                'syncsSent': 0,
                'syncsReceived': 0,
                'lastSyncAt': datetime.now().isoformat(),
                'connectionsActive': 0,
                'databaseEntries': 0,
                'nodeId': 'unavailable',
                'memory': {
                    'heapTotal': '0 MB',
                    'heapUsed': '0 MB',
                    'rss': '0 MB',
                    'external': '0 MB'
                }
            },
            enabled=False
        )

@database_bp.route('/phalanxdb/test_hash', methods=['POST'])
@admin_required
def test_phalanx_hash():
    """Test a specific hash against PhalanxDB"""
    try:
        hash_value = request.form.get('hash', '').strip()
        if not hash_value:
            return jsonify({'error': 'No hash provided'}), 400
            
        # Initialize cache manager
        phalanx_manager = PhalanxDBClassManager()
        
        # Get cache status
        result = phalanx_manager.get_cache_status(hash_value)
        
        if result is None:
            return jsonify({
                'status': 'not_found',
                'message': 'Hash not found in database'
            })
            
        # Format the timestamps for display
        if result.get('timestamp'):
            result['timestamp'] = result['timestamp'].strftime('%Y-%m-%d %H:%M:%S UTC')
        if result.get('expiry'):
            result['expiry'] = result['expiry'].strftime('%Y-%m-%d %H:%M:%S UTC')
            
        return jsonify({
            'status': 'success',
            'data': result
        })
        
    except Exception as e:
        logging.error(f"Error testing hash: {str(e)}")
        return jsonify({'error': str(e)}), 500

@database_bp.route('/visual')
@admin_required
def visual_browser():
    """Render the visual database browser page."""
    return render_template('database_visual.html')

@database_bp.route('/visual_data')
@admin_required
def visual_data():
    """Fetch data formatted for the visual browser, grouped by unique media, with pagination and search."""
    conn = None
    try:
        # Get limit, offset, and search term from query parameters
        limit = request.args.get('limit', default=50, type=int)
        offset = request.args.get('offset', default=0, type=int)
        search_term = request.args.get('search', default='', type=str).strip()
        limit = max(1, min(limit, 200))

        from database import get_db_connection
        conn = get_db_connection()
        cursor = conn.cursor()

        columns = ['MIN(id) as id', 'title', 'year', 'imdb_id', 'tmdb_id', 'type']
        columns_str = ", ".join(columns)
        output_columns = ['id', 'title', 'year', 'imdb_id', 'tmdb_id', 'type']

        # Parameters for the query
        params = []

        # WHERE clause for search (apply before grouping)
        where_clause = ""
        if search_term:
            where_clause = "WHERE title LIKE ?"
            params.append(f'%{search_term}%')

        # Base query structure (including potential WHERE clause)
        # Improved GROUP BY to prioritize non-null IDs
        base_query = f"""
            FROM media_items
            {where_clause}
            GROUP BY
                CASE
                    WHEN imdb_id IS NOT NULL AND imdb_id != '' THEN imdb_id
                    WHEN tmdb_id IS NOT NULL AND tmdb_id != '' THEN CAST(tmdb_id AS TEXT) -- Cast tmdb_id to TEXT for concatenation
                    ELSE title || '-' || year
                END
        """

        # Query to get the current batch of items
        query = f"""
            SELECT {columns_str}
            {base_query}
            ORDER BY title, year
            LIMIT ? OFFSET ?
        """

        # Add limit and offset to parameters
        query_params = params + [limit, offset]
        logging.debug(f"Executing visual data query: {query} with params {query_params}")
        cursor.execute(query, query_params)
        items_raw = cursor.fetchall()
        logging.debug(f"Fetched {len(items_raw)} raw items")

        # Process items to add poster path
        items = []
        for row in items_raw:
            item_dict = dict(zip(output_columns, row))
            # Fetch media metadata including poster path
            tmdb_id = item_dict.get('tmdb_id')
            media_type = item_dict.get('type')
            poster_path = '/static/images/placeholder.png' # Default placeholder

            if tmdb_id and media_type:
                logging.debug(f"Fetching metadata for TMDB ID: {tmdb_id}, Type: {media_type}")
                try:
                    # Use get_media_meta to leverage caching and TMDB API (if available)
                    media_meta = get_media_meta(tmdb_id, media_type)
                    if media_meta and media_meta[0]: # Check if poster_url (index 0) exists
                        poster_path = media_meta[0]
                        logging.debug(f"Got poster path: {poster_path}")
                    else:
                        logging.debug(f"No poster path found in metadata for {tmdb_id}")
                        poster_path = '/static/images/placeholder.png' # Ensure placeholder if metadata lacks poster

                except Exception as meta_error:
                    logging.error(f"Error fetching metadata for TMDB ID {tmdb_id}, Type {media_type}: {meta_error}", exc_info=True)
                    poster_path = '/static/images/placeholder.png' # Ensure placeholder on error
            else:
                logging.debug(f"Skipping metadata fetch for item: {item_dict.get('title')}, TMDB ID: {tmdb_id}, Type: {media_type}")

            item_dict['poster_path'] = poster_path
            items.append(item_dict)

        # Query to check if there are more items beyond the current batch
        more_check_query = f"""
            SELECT 1
            {base_query}
            ORDER BY title, year
            LIMIT 1 OFFSET ?
        """
        more_check_params = params + [offset + limit]
        logging.debug(f"Executing more check query: {more_check_query} with params {more_check_params}")
        cursor.execute(more_check_query, more_check_params)
        has_more = cursor.fetchone() is not None
        logging.debug(f"Has more items: {has_more}")

        return jsonify({'success': True, 'items': items, 'has_more': has_more})

    except sqlite3.Error as e:
        logging.error(f"SQLite error in visual_data route: {str(e)}")
        return jsonify({'success': False, 'error': f"Database error: {str(e)}"}), 500
    except Exception as e:
        logging.error(f"Unexpected error in visual_data route: {str(e)}", exc_info=True) # Log full traceback
        return jsonify({'success': False, 'error': "An unexpected error occurred."}), 500
    finally:
        if conn:
            conn.close()