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

@database_bp.route('/', methods=['GET', 'POST'])
@admin_required
def index():
    request_start_time = time.perf_counter() # Start timer for the whole request
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
        logging.info("Using cached statistics summary.")
        counts = cached_stats_data
    else:
        logging.info("Fetching fresh statistics summary.")
        from database.statistics import get_statistics_summary
        counts = get_statistics_summary()
        cached_stats_data = counts
        stats_cache_timestamp = current_time
        logging.info(f"Statistics summary cached for {STATS_CACHE_DURATION_SECONDS} seconds.")

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

        cursor.execute("PRAGMA table_info(media_items)")
        db_actual_columns = [column[1] for column in cursor.fetchall()]
        
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
        cursor.execute(query, final_params)
        items_from_db = cursor.fetchall()
        logging.debug(f"Fetched {len(items_from_db)} items from the database")
        
        items_dict_list = [dict(zip(final_columns_for_sql_query_list, item_row)) for item_row in items_from_db]

        if needs_size_data:
            for item_dict in items_dict_list:
                loc = item_dict.get('location_on_disk')
                orig_path = item_dict.get('original_path_for_symlink')
                item_dict['size_gb'] = get_item_size_gb(loc, orig_path)
        
        if sort_column_req == 'size':
            items_dict_list.sort(key=lambda x: x.get('size_gb', 0.0), reverse=(sort_order_req == 'desc'))

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

        data.update({
            'items': items_dict_list,
            # 'selected_columns' is already updated with current_selected_columns_for_display
            # 'all_columns' is already updated with all_columns_for_ui
            'filters': filters, 
            # 'sort_column' and 'sort_order' are already updated with validated ones
            'current_letter': current_letter_for_template,
            'content_type': content_type_for_template,
            'filter_logic': filter_logic,
            'column_values': column_values,
        })

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
def bulk_queue_action():
    action = request.form.get('action')
    target_queue = request.form.get('target_queue')
    selected_items = request.form.getlist('selected_items')
    blacklist = request.form.get('blacklist', 'false').lower() == 'true' if action == 'delete' else False

    logging.info(f"Bulk action route called. Action: '{action}', Items: {selected_items[:5]}...")

    if not action or not selected_items:
        logging.warning("Bulk action returning error: Action or selected items missing.")
        return jsonify({'success': False, 'error': 'Action and selected items are required'})

    BATCH_SIZE = 450
    total_processed = 0
    error_count = 0
    errors = []

    from database import get_db_connection

    try:
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
                            else:
                                success = response.json.get('success', False)
                                
                            if success:
                                total_processed += 1
                            else:
                                error_count += 1
                                error_msg = response.json.get('error', 'Unknown error')
                                errors.append(f"Error processing item {item_id}: {error_msg}")
                                
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
                except Exception as e:
                    error_count += 1
                    conn.rollback()
                    errors.append(f"Error in batch {i//BATCH_SIZE + 1}: {str(e)}")
                    logging.error(f"Error in batch {i//BATCH_SIZE + 1}: {str(e)}")
                finally:
                    conn.close()
            elif action == 'rescrape':
                logging.info(f"Entering 'rescrape' block for batch: {batch}")
                # --- New Rescrape Logic ---
                # Get file management settings once per batch
                file_management = get_setting('File Management', 'file_collection_management', 'Plex')
                mounted_location = get_setting('Plex', 'mounted_file_location', get_setting('File Management', 'original_files_path', ''))
                original_files_path = get_setting('File Management', 'original_files_path', '')
                symlinked_files_path = get_setting('File Management', 'symlinked_files_path', '')
                plex_url_for_symlink = get_setting('File Management', 'plex_url_for_symlink', '') # Get setting for symlinked Plex

                for item_id in batch:
                    item = None # Reset item for each iteration
                    try:
                        logging.info(f"Rescrape: Processing item_id: {item_id}")
                        item = get_media_item_by_id(item_id)
                        if not item:
                            errors.append(f"Item {item_id} not found.")
                            error_count += 1
                            logging.warning(f"Rescrape: Item {item_id} not found, skipping.")
                            continue

                        # 1. Handle File Deletion
                        if item['state'] in ['Collected', 'Upgrading']: # Only delete if currently collected/upgrading
                             if file_management == 'Plex':
                                 if mounted_location and item.get('location_on_disk'):
                                     if os.path.exists(item['location_on_disk']):
                                         os.remove(item['location_on_disk'])
                                         logging.info(f"Rescrape: Deleted Plex file: {item['location_on_disk']}")

                             elif file_management == 'Symlinked/Local':
                                 # Handle symlink removal
                                 if item.get('location_on_disk'):
                                     if os.path.exists(item['location_on_disk']) and os.path.islink(item['location_on_disk']):
                                         os.unlink(item['location_on_disk'])
                                         logging.info(f"Rescrape: Removed symlink: {item['location_on_disk']}")
                                 # Handle original file removal
                                 if item.get('original_path_for_symlink'):
                                     if os.path.exists(item['original_path_for_symlink']):
                                         os.remove(item['original_path_for_symlink'])
                                         logging.info(f"Rescrape: Deleted original file: {item['original_path_for_symlink']}")

                        sleep(0.5) # Small delay before Plex removal

                        # 2. Remove from Plex (if applicable)
                        if item['state'] in ['Collected', 'Upgrading']:
                             if file_management == 'Plex' and item.get('filled_by_file'):
                                 if item['type'] == 'movie':
                                     remove_file_from_plex(item['title'], item['filled_by_file'])
                                 elif item['type'] == 'episode':
                                     remove_file_from_plex(item['title'], item['filled_by_file'], item.get('episode_title')) # Use .get for safety
                             elif file_management == 'Symlinked/Local' and plex_url_for_symlink and item.get('location_on_disk'):
                                 if item['type'] == 'movie':
                                     remove_file_from_plex(item['title'], os.path.basename(item['location_on_disk']))
                                 elif item['type'] == 'episode':
                                     remove_file_from_plex(item['title'], os.path.basename(item['location_on_disk']), item.get('episode_title'))

                        # 3. Update Database State to 'Wanted' and clear file info
                        logging.info(f"Rescrape: Attempting to update item {item_id} state to 'Wanted'. Current state: {item.get('state')}")
                        update_media_item_state(
                            item_id,
                            'Wanted',
                            location_on_disk=None,
                            original_path_for_symlink=None,
                            filled_by_file=None,
                        )
                        logging.info(f"Rescrape: Successfully called update_media_item_state for item {item_id}.")
                        total_processed += 1

                    except Exception as e:
                        error_count += 1
                        error_msg = f"Error processing item {item_id} for rescrape: {str(e)}"
                        errors.append(error_msg)
                        logging.error(f"Rescrape: {error_msg}", exc_info=True)
                # --- End New Rescrape Logic ---

            else:
                logging.warning(f"Bulk action returning error: Invalid action '{action}'")
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
                "rescrape": "deleted files/Plex entries for and moved to Wanted queue" # Added rescrape message
            }
            action_text = action_map.get(action, f"processed ({action})")
            message = f"Successfully {action_text} {total_processed} items"
            return jsonify({'success': True, 'message': message})

    except Exception as e:
        logging.error(f"Outer exception in bulk action '{action}': {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': f"An unexpected error occurred during bulk {action}: {str(e)}"})

@database_bp.route('/delete_item', methods=['POST'])
def delete_item():
    data = request.json
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

        # Handle file deletion based on management type
        if file_management == 'Plex' and (item['state'] == 'Collected' or item['state'] == 'Upgrading'):
            if mounted_location and item.get('location_on_disk'):
                try:
                    if os.path.exists(item['location_on_disk']):
                        os.remove(item['location_on_disk'])
                except Exception as e:
                    logging.error(f"Error deleting file at {item['location_on_disk']}: {str(e)}")

            sleep(1)

            if item['type'] == 'movie':
                remove_file_from_plex(item['title'], item['filled_by_file'])
            elif item['type'] == 'episode':
                remove_file_from_plex(item['title'], item['filled_by_file'], item['episode_title'])

        elif file_management == 'Symlinked/Local' and (item['state'] == 'Collected' or item['state'] == 'Upgrading'):
            # Handle symlink removal
            if item.get('location_on_disk'):
                try:
                    if os.path.exists(item['location_on_disk']) and os.path.islink(item['location_on_disk']):
                        os.unlink(item['location_on_disk'])
                except Exception as e:
                    logging.error(f"Error removing symlink at {item['location_on_disk']}: {str(e)}")

            # Handle original file removal
            if item.get('original_path_for_symlink'):
                try:
                    if os.path.exists(item['original_path_for_symlink']):
                        os.remove(item['original_path_for_symlink'])
                except Exception as e:
                    logging.error(f"Error deleting original file at {item['original_path_for_symlink']}: {str(e)}")

            sleep(1)

            # Remove from Plex if configured
            plex_url = get_setting('File Management', 'plex_url_for_symlink', '')
            if plex_url:
                if item['type'] == 'movie':
                    remove_file_from_plex(item['title'], os.path.basename(item['location_on_disk']))
                elif item['type'] == 'episode':
                    remove_file_from_plex(item['title'], os.path.basename(item['location_on_disk']), item['episode_title'])

        # Handle database operation based on blacklist flag
        if blacklist:
            from database import update_media_item_state
            update_media_item_state(item_id, 'Blacklisted')
        else:
            from database import remove_from_media_items
            remove_from_media_items(item_id)

        return jsonify({'success': True})

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
def reverse_parser():
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
def apply_parsed_versions():
    try:
        from database import get_all_media_items
        items = get_all_media_items()
        updated_count = 0
        for item in items:
            if item['filled_by_file']:
                parsed_version = parse_filename_for_version(item['filled_by_file'])
                
                # Only update if the parsed version is different from the current version
                current_version = item['version'] if 'version' in item.keys() else None
                if parsed_version != current_version:
                    try:
                        from database import update_media_item_state
                        update_media_item_state(item['id'], item['state'], version=parsed_version)
                        updated_count += 1
                    except Exception as e:
                        logging.error(f"Error updating item {item['id']}: {str(e)}")
        
        return jsonify({
            'success': True, 
            'message': f'Parsed versions applied successfully. Updated {updated_count} items.'
        })
    except Exception as e:
        logging.error(f"Error applying parsed versions: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

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
        
    except Exception as e:
        logging.error(f"Error clearing watch history: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})

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