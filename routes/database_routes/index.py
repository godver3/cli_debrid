"""Database browser page with cached collection stats."""

from flask import jsonify, request, render_template, session, flash
import sqlite3
import string
import logging
import json
from ..models import admin_required
import time
from queues.config_manager import get_content_source_display_names
from ._common import PER_PAGE, STATS_CACHE_DURATION_SECONDS, database_bp, get_basic_collection_counts, item_source_label, source_filter_clause

def unique_key_count_sql(columns):
    """SQL for the number of distinct items, matching consolidate_items' key.

    Versions of one movie (title + year) or one episode (title + year + SxE)
    count once, as on the Queues page.
    """
    def col(name):
        return f"COALESCE(CAST(\"{name}\" AS TEXT), '')" if name in columns else "''"
    movie_key = f"{col('title')} || '_' || {col('year')}"
    if 'type' not in columns:
        return f"COUNT(DISTINCT {movie_key})"
    episode_key = f"{movie_key} || '_S' || {col('season_number')} || 'E' || {col('episode_number')}"
    return (f"COUNT(DISTINCT CASE WHEN \"type\" = 'episode' THEN 'e:' || {episode_key} "
            f"ELSE 'm:' || {movie_key} END)")


# Module-level cache for statistics
cached_stats_data = None

stats_cache_timestamp = 0

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
        'current_letter': '',
        'content_type': 'all',
        'filter_logic': 'AND',
        'column_values': {},
        'operators': [
            {'value': 'contains', 'label': 'Contains'},
            {'value': 'not_contains', 'label': 'Not Contains'},
            {'value': 'equals', 'label': 'Equals'},
            {'value': 'not_equals', 'label': 'Not Equals'},
            {'value': 'starts_with', 'label': 'Starts With'},
            {'value': 'ends_with', 'label': 'Ends With'},
            {'value': 'greater_than', 'label': 'Greater Than'},
            {'value': 'less_than', 'label': 'Less Than'},
            {'value': 'is_null', 'label': 'Is Null'},
            {'value': 'is_not_null', 'label': 'Is Not Null'}
        ],
        'content_source_display_map': {},
        'current_page': 1,
        'per_page': PER_PAGE,
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
        # 'source' is computed per row: Usenet or Debrid (from filled_by_torrent_id).
        if 'source' not in all_columns_for_ui:
            all_columns_for_ui.append('source')
        data['all_columns'] = all_columns_for_ui

        default_display_columns = ['id', 'title', 'year', 'type', 'state', 'version', 'source']

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
            # Source was added after column choices were saved: show it once by default.
            if raw_selected_columns and 'source' not in raw_selected_columns and not session.get('source_column_offered'):
                raw_selected_columns = list(raw_selected_columns) + ['source']
                session['selected_columns'] = raw_selected_columns
                session['source_column_offered'] = True

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

        logging.info(f"Received params - content_type: {repr(content_type_req)}, letter: {repr(current_letter_req)}")

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
        
        # Continue with setting up SQL query columns. type/tmdb_id/imdb_id are always
        # read for the row's Open button; filled_by_torrent_id/debrid_provider for Source.
        columns_for_sql_query = set(['id'])
        for _col in ('type', 'tmdb_id', 'imdb_id', 'filled_by_torrent_id', 'debrid_provider'):
            if _col in db_actual_columns:
                columns_for_sql_query.add(_col)
        for col in current_selected_columns_for_display: # Use the finalized display columns
            if col in db_actual_columns:  # Include size if it exists in DB
                columns_for_sql_query.add(col)

        needs_size_data = (sort_column_req == 'size' or 'size' in current_selected_columns_for_display)
        if needs_size_data:
            # Always include size column if it exists in DB
            if 'size' in db_actual_columns:
                columns_for_sql_query.add('size')
            # Also include location fields for fallback calculation if size is NULL
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

                # Size column CAN be filtered if it exists in the database as a real column
                if column == 'source':
                    if operator not in [op['value'] for op in data['operators']]:
                        continue
                    _src = source_filter_clause(db_actual_columns, operator, raw_value)
                    if _src:
                        filter_where_clauses.append(_src[0])
                        filter_params.extend(_src[1])
                    continue

                if column == 'size' and 'size' not in db_actual_columns:
                    logging.warning(f"Ignoring filter on 'size' column: '{column}' as it doesn't exist in database.")
                    continue

                if raw_value == '' and operator in ['contains', 'not_contains', 'starts_with', 'ends_with', 'greater_than', 'less_than']:
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
                if column in ('state', 'type') and operator == 'not_contains':
                    operator = 'not_equals'

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
                    
                    # Special handling for ghostlisted state filter
                    if column == 'state' and value == 'ghostlisted':
                        filter_where_clauses.append('ghostlisted = TRUE')
                        clause_added_in_this_iteration = True
                    # Special handling for all_blacklisted state filter
                    elif column == 'state' and value == 'all_blacklisted':
                        from database.database_reading import get_items_with_all_blacklisted_versions
                        all_blacklisted_ids = get_items_with_all_blacklisted_versions()
                        if all_blacklisted_ids:
                            placeholders = ','.join('?' * len(all_blacklisted_ids))
                            filter_where_clauses.append(f'id IN ({placeholders})')
                            filter_params.extend(all_blacklisted_ids)
                        else:
                            # If no items have all versions blacklisted, return no results
                            filter_where_clauses.append('1 = 0')
                        clause_added_in_this_iteration = True
                    elif operator == 'contains': filter_where_clauses.append(f'"{column}" LIKE ?'); filter_params.append(f"%{value}%")
                    elif operator == 'not_contains': filter_where_clauses.append(f'("{column}" IS NULL OR "{column}" NOT LIKE ?)'); filter_params.append(f"%{value}%")
                    elif operator == 'equals': filter_where_clauses.append(f'"{column}" = ?'); filter_params.append(value)
                    elif operator == 'not_equals': filter_where_clauses.append(f'"{column}" IS NOT ?'); filter_params.append(value) # Changed to IS NOT
                    elif operator == 'starts_with': filter_where_clauses.append(f'"{column}" LIKE ?'); filter_params.append(f"{value}%")
                    elif operator == 'ends_with': filter_where_clauses.append(f'"{column}" LIKE ?'); filter_params.append(f"%{value}")
                    elif operator == 'greater_than':
                        try: 
                            float_value = float(value)
                            filter_where_clauses.append(f'CAST("{column}" AS REAL) > ?'); 
                            filter_params.append(float_value)
                        except (ValueError, TypeError): 
                            filter_where_clauses.append(f'"{column}" > ?'); 
                            filter_params.append(value)
                    elif operator == 'less_than':
                        try: 
                            float_value = float(value)
                            filter_where_clauses.append(f'CAST("{column}" AS REAL) < ?'); 
                            filter_params.append(float_value)
                        except (ValueError, TypeError): 
                            filter_where_clauses.append(f'"{column}" < ?'); 
                            filter_params.append(value)
                    if len(filter_where_clauses) > original_clause_count: clause_added_in_this_iteration = True; continue
        
        final_where_clause = ""
        final_params = []
        effective_content_type = content_type_req if content_type_req is not None else 'all'
        effective_current_letter = current_letter_req if current_letter_req is not None else ''

        logging.info(f"Effective values - content_type: {repr(effective_content_type)}, letter: {repr(effective_current_letter)}")

        # Check if user is specifically filtering for ghostlisted items
        show_ghostlisted = any(
            f.get('column') == 'state' and f.get('value') == 'ghostlisted' 
            for f in filters
        )

        # Build default clauses for content_type and letter (always apply these)
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

        # Combine default clauses (content_type + letter) with user filters
        all_clauses = default_clauses + filter_where_clauses
        all_params = default_params + filter_params

        if all_clauses:
            if filter_where_clauses:
                # If there are user filters, combine them with default clauses
                filter_combination_operator = f" {filter_logic} "
                combined_filter_clause = filter_combination_operator.join(filter_where_clauses)
                # AND the default clauses with the combined filter clause
                final_where_clause = "WHERE " + " AND ".join(default_clauses + [f"({combined_filter_clause})"])
            else:
                # Only default clauses (no user filters)
                final_where_clause = "WHERE " + " AND ".join(default_clauses)
            final_params = all_params
        else:
            final_where_clause = ""
            final_params = []

        # Always use the effective values for the template
        content_type_for_template = effective_content_type
        current_letter_for_template = effective_current_letter

        logging.info(f"Template values - content_type: {repr(content_type_for_template)}, letter: {repr(current_letter_for_template)}")

        # Add ghostlisted filter unless user is specifically looking for ghostlisted items
        if not show_ghostlisted:
            if final_where_clause:
                final_where_clause = final_where_clause.replace("WHERE ", "WHERE (ghostlisted = FALSE OR ghostlisted IS NULL) AND (")
                final_where_clause = final_where_clause + ")"
            else:
                final_where_clause = "WHERE (ghostlisted = FALSE OR ghostlisted IS NULL)"
        
        order_clause = ""
        if sort_column_req == 'size':
            # Missing sizes sort as 0; ties keep row order (as the old stable
            # Python sort did, in both directions)
            if 'size' in db_actual_columns:
                order_clause = f'ORDER BY COALESCE("size", 0.0) {sort_order_req}, rowid ASC'
        elif sort_column_req == 'source' and 'filled_by_torrent_id' in db_actual_columns:
            # Usenet, then debrid, then none (or reversed)
            order_clause = (f"ORDER BY CASE WHEN filled_by_torrent_id LIKE 'nzb:%' THEN 0 "
                            f"WHEN COALESCE(filled_by_torrent_id, '') != '' THEN 1 ELSE 2 END {sort_order_req}, rowid ASC")
        elif sort_column_req in db_actual_columns:
            order_clause = f'ORDER BY "{sort_column_req}" {sort_order_req}'

        # Paginate in SQL: this page used to fetch and convert every matching
        # row on each infinite-scroll request, then slice one page out.
        timings['filter_processing_done'] = time.perf_counter()

        if request.args.get('ids_only') == '1':
            # "Select all N matching": every id the current view matches, in view order.
            cursor.execute(f"SELECT id FROM media_items {final_where_clause} {order_clause}", final_params)
            return jsonify({'ids': [str(row[0]) for row in cursor.fetchall()]})

        cursor.execute(f"SELECT COUNT(*), {unique_key_count_sql(db_actual_columns)} "
                       f"FROM media_items {final_where_clause}", final_params)
        total_count, unique_total_count = cursor.fetchone()
        try:
            page = int(request.args.get('page', 1))
        except (ValueError, TypeError):
            page = 1

        _VALID_PER_PAGE = {100, 250, 500, 1000, 2000, 5000}
        try:
            per_page = int(request.args.get('per_page', PER_PAGE))
            if per_page not in _VALID_PER_PAGE:
                per_page = PER_PAGE
        except (ValueError, TypeError):
            per_page = PER_PAGE
        total_pages = max(1, (total_count + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))  # Clamp to valid range
        start_idx = (page - 1) * per_page

        query = f"{base_query} {final_where_clause} {order_clause} LIMIT ? OFFSET ?"
        logging.debug(f"Executing query: {query} with params: {final_params}")
        cursor.execute(query, list(final_params) + [per_page, start_idx])
        items_from_db = cursor.fetchall()
        logging.debug(f"Fetched {len(items_from_db)} items from the database")
        timings['main_query_executed'] = time.perf_counter()
        
        items_dict_list = [dict(zip(final_columns_for_sql_query_list, item_row)) for item_row in items_from_db]
        for item_dict in items_dict_list:
            item_dict['source'] = item_source_label(item_dict)

        if needs_size_data:
            # PERFORMANCE FIX: Only use database size values, skip slow disk I/O
            # Disk-based size calculation with os.path.exists() and os.path.getsize()
            # is extremely slow for symlink mode with remote/mounted filesystems.
            # This was causing multi-second delays when displaying size column.
            for item_dict in items_dict_list:
                db_size = item_dict.get('size')
                if db_size is not None:
                    item_dict['size_gb'] = db_size
                else:
                    # Don't calculate from disk - it's too slow for remote filesystems
                    # Size will be populated when item is collected/processed
                    item_dict['size_gb'] = 0.0

        timings['item_data_processing_done'] = time.perf_counter()

        # OPTIMIZATION: Column values are now lazy-loaded on-demand via /database/get_column_values/<column>
        # This significantly improves initial page load performance
        column_values = {}  # Empty dict - values fetched on-demand by frontend
        logging.info("Column values will be lazy-loaded on-demand (performance optimization)")
        timings['distinct_values_skipped'] = time.perf_counter()

        end_idx = start_idx + len(items_dict_list)
        paginated_items = items_dict_list

        logging.info(f"Pagination: page {page}/{total_pages}, items {start_idx+1}-{end_idx} of {total_count}")

        data.update({
            'items': paginated_items,
            'result_count': len(paginated_items),
            'total_count': total_count,
            'current_page': page,
            'total_pages': total_pages,
            'per_page': per_page,
            'filters': filters,
            'current_letter': current_letter_for_template,
            'content_type': content_type_for_template,
            'filter_logic': filter_logic,
            'column_values': column_values,
            # Over the whole filter, not just this page (versions of one item count once).
            'unique_result_count': unique_total_count,
        })

        timings['data_updated_for_template'] = time.perf_counter()

        # Calculate and log durations with performance warnings
        timing_log = "Database index route timing breakdown:\n"
        last_timing = request_start_time
        slow_operations = []
        for key, timestamp in timings.items():
            if key != 'overall_start':
                duration = timestamp - last_timing
                timing_log += f"  - {key}: {duration:.4f} seconds\n"
                # Flag operations taking longer than 1 second
                if duration > 1.0:
                    slow_operations.append(f"{key} ({duration:.2f}s)")
                last_timing = timestamp
        total_duration = time.perf_counter() - request_start_time
        timing_log += f"Total processing time for request: {total_duration:.4f} seconds."

        if slow_operations:
            logging.warning(f"PERFORMANCE WARNING - Slow operations detected: {', '.join(slow_operations)}")

        if total_duration > 2.0:
            logging.warning(f"PERFORMANCE WARNING - Slow request: {total_duration:.2f}s total")
            logging.info(timing_log)
        else:
            logging.info(timing_log)
        data['timings'] = {k: v - request_start_time for k, v in timings.items() if k != 'overall_start'} # Relative timings for template
        data['total_request_time'] = total_duration

        if request.args.get('ajax') == '1':
            logging.info(f"Database index route finished (AJAX). Total time: {time.perf_counter() - request_start_time:.4f} seconds.")
            return jsonify(data)
        else:
            logging.info(f"Database index route finished (HTML). Total time: {time.perf_counter() - request_start_time:.4f} seconds.")
            return render_template('database.html', **data)

    except sqlite3.OperationalError as e:
        logging.error(f"SQLite operational error in database route: {str(e)}")
        if "database is locked" in str(e).lower():
            error_message = "The database is currently busy processing another request. Please try again in a few moments."
            status_code = 503
        else:
            error_message = f"Database operation failed: {str(e)}. Please check your filters and try again."
            status_code = 500
        logging.info(f"Database index route finished with SQLite operational error. Total time: {time.perf_counter() - request_start_time:.4f} seconds.")
        if request.args.get('ajax') == '1':
            return jsonify({'error': error_message, 'database_locked': 'locked' in str(e).lower()}), status_code
        else:
            flash(error_message, "error")
            return render_template('database.html', **data)
    except sqlite3.Error as e:
        logging.error(f"SQLite error in database route: {str(e)}")
        error_message = f"A database error occurred. This might be due to invalid filter values or a database issue. Please check your input and try again."
        logging.info(f"Database index route finished with SQLite error. Total time: {time.perf_counter() - request_start_time:.4f} seconds.")
        if request.args.get('ajax') == '1':
            return jsonify({'error': error_message}), 500
        else:
            flash(error_message, "error")
            return render_template('database.html', **data)
    except Exception as e:
        logging.error(f"Unexpected error in database route: {str(e)}", exc_info=True)
        error_message = "An unexpected error occurred while loading the database page. Please refresh the page and try again. If the problem persists, check the logs."
        logging.info(f"Database index route finished with unexpected error. Total time: {time.perf_counter() - request_start_time:.4f} seconds.")
        if request.args.get('ajax') == '1':
            return jsonify({'error': error_message}), 500
        else:
            flash(error_message, "error")
            return render_template('database.html', **data) 
    finally:
        if conn:
            conn.close()
