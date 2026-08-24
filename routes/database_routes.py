from flask import jsonify, request, render_template, session, flash, Blueprint, current_app
import sqlite3
import string
import logging
from sqlalchemy import text, inspect
from routes.extensions import db
from utilities.settings import get_setting
import json
from utilities.reverse_parser import get_version_settings, get_default_version, get_version_order, parse_filename_for_version
from .models import admin_required, user_required
from utilities.plex_removal_cache import cache_plex_removal
from utilities.plex_functions import remove_file_from_plex, scan_and_empty_plex_trash
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
from database.database_writing import update_media_item, RESET_COLLECTION_STATE_SQL
from database.symlink_verification import add_symlinked_file_for_verification
database_bp = Blueprint('database', __name__)

def translate_plex_path_to_local(plex_path: str) -> str:
    """
    Translate a Plex path to the local filesystem path.

    Plex stores paths as it sees them (e.g., /movies/Title/file.mkv),
    but the local filesystem may have these at a different location
    (e.g., /mnt/symlinked/movies/Title/file.mkv).

    This function attempts to find the local path by checking if the
    plex_path exists, and if not, prepending the symlinked_files_path.

    Args:
        plex_path: The path as stored in location_on_disk (Plex's view)

    Returns:
        The local filesystem path where the symlink actually exists
    """
    if not plex_path:
        return plex_path

    # First, check if the path already exists (it might already be a local path)
    if os.path.exists(plex_path) or os.path.islink(plex_path):
        return plex_path

    # Get the symlinked files path setting
    symlinked_files_path = get_setting('File Management', 'symlinked_files_path', '')
    if not symlinked_files_path:
        logging.debug(f"No symlinked_files_path setting found, returning original path: {plex_path}")
        return plex_path

    # Try to construct the local path by prepending symlinked_files_path
    # The plex_path might be something like /movies/Title/file.mkv
    # and we need /mnt/symlinked/movies/Title/file.mkv

    # Remove leading slash from plex_path if present for joining
    relative_path = plex_path.lstrip('/')
    local_path = os.path.join(symlinked_files_path, relative_path)

    if os.path.exists(local_path) or os.path.islink(local_path):
        logging.debug(f"Translated Plex path '{plex_path}' to local path '{local_path}'")
        return local_path

    # If that didn't work, try matching by filename in the symlinked directory
    # This handles cases where folder structure differs between Plex and local
    filename = os.path.basename(plex_path)

    # Try to find the file recursively in symlinked_files_path (limited depth)
    try:
        for root, dirs, files in os.walk(symlinked_files_path):
            # Limit depth to avoid searching too deep
            depth = root.replace(symlinked_files_path, '').count(os.sep)
            if depth > 4:  # Max 4 levels deep
                dirs[:] = []  # Don't recurse further
                continue
            if filename in files:
                found_path = os.path.join(root, filename)
                if os.path.islink(found_path):
                    logging.debug(f"Found symlink by filename search: '{found_path}' for Plex path '{plex_path}'")
                    return found_path
    except Exception as e:
        logging.debug(f"Error searching for file in symlinked_files_path: {e}")

    # Return original path if no translation found
    logging.debug(f"Could not translate Plex path '{plex_path}', returning as-is")
    return plex_path

# Configuration Constants
BATCH_SIZE = 450  # Number of items to process in each batch
PER_PAGE = 250  # Number of items per page for pagination
STATS_CACHE_DURATION_SECONDS = 60  # Cache statistics for 60 seconds

# Module-level cache for statistics
cached_stats_data = None
stats_cache_timestamp = 0

def queue_plex_removal_for_item(item_db_data, file_management, item_id):
    """Helper function to queue Plex removal for an item based on file management mode.

    Args:
        item_db_data: Dictionary containing item data from database
        file_management: String indicating file management mode ('Plex' or 'Symlinked/Local')
        item_id: ID of the item being processed
    """
    if file_management == 'Plex' and item_db_data.get('filled_by_file'):
        if item_db_data['type'] == 'movie':
            cache_plex_removal(item_db_data['title'], item_db_data['filled_by_file'])
        elif item_db_data['type'] == 'episode':
            cache_plex_removal(item_db_data['title'], item_db_data['filled_by_file'], item_db_data.get('episode_title'))
        logging.info(f"Rescrape: Queued Plex removal for item {item_id} (Plex mode).")
    elif file_management == 'Symlinked/Local' and item_db_data.get('location_on_disk'):
        # Path for symlinked items should be location_on_disk, which is the symlink path
        path_to_remove = item_db_data['location_on_disk']
        if item_db_data['type'] == 'movie':
            cache_plex_removal(item_db_data['title'], path_to_remove)
        elif item_db_data['type'] == 'episode':
            cache_plex_removal(item_db_data['title'], path_to_remove, item_db_data.get('episode_title'))
        logging.info(f"Rescrape: Queued Plex removal for item {item_id} (Symlinked/Local mode with Plex URL). Path: {path_to_remove}")

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

        default_display_columns = ['id', 'title', 'year', 'type', 'state', 'version']

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
        
        # Continue with setting up SQL query columns
        columns_for_sql_query = set(['id'])
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
        # SQL sorting only if not sorting by 'size' and sort_column is a real DB column
        if sort_column_req != 'size' and sort_column_req in db_actual_columns:
            order_clause = f'ORDER BY "{sort_column_req}" {sort_order_req}'
        
        # Load all data immediately - no artificial limits
        query = f"{base_query} {final_where_clause} {order_clause}"
        logging.debug(f"Executing query: {query} with params: {final_params}")
        timings['filter_processing_done'] = time.perf_counter()
        cursor.execute(query, final_params)
        items_from_db = cursor.fetchall()
        logging.debug(f"Fetched {len(items_from_db)} items from the database")
        timings['main_query_executed'] = time.perf_counter()
        
        items_dict_list = [dict(zip(final_columns_for_sql_query_list, item_row)) for item_row in items_from_db]

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
        
        if sort_column_req == 'size':
            items_dict_list.sort(key=lambda x: x.get('size_gb', 0.0), reverse=(sort_order_req == 'desc'))
        timings['item_data_processing_done'] = time.perf_counter()

        # OPTIMIZATION: Column values are now lazy-loaded on-demand via /database/get_column_values/<column>
        # This significantly improves initial page load performance
        column_values = {}  # Empty dict - values fetched on-demand by frontend
        logging.info("Column values will be lazy-loaded on-demand (performance optimization)")
        timings['distinct_values_skipped'] = time.perf_counter()

        # Pagination: items per page for infinite scroll
        total_count = len(items_dict_list)
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
        end_idx = min(start_idx + per_page, total_count)
        paginated_items = items_dict_list[start_idx:end_idx]

        logging.info(f"Pagination: page {page}/{total_pages}, items {start_idx+1}-{end_idx} of {total_count}")

        from routes.queues_routes import consolidate_items
        unique_items, _ = consolidate_items(paginated_items)
        timings['items_consolidated'] = time.perf_counter()

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
            'unique_result_count': len(unique_items),
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

    batch_size = BATCH_SIZE
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


        for i in range(0, len(selected_items), batch_size):
            batch = selected_items[i:i + batch_size]
            logging.info(f"Processing batch {i//batch_size + 1}. Action: '{action}'")

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
                        errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                        logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
                except Exception as e:
                    error_count += 1
                    conn.rollback()
                    errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                    logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
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
                        errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                        logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
                except Exception as e:
                    error_count += 1
                    conn.rollback()
                    errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                    logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
                finally:
                    conn.close()
            elif action == 'assign_tags' and target_queue:  # target_queue contains the tag to assign in this case
                logging.info("Entering 'assign_tags' block.")
                conn = get_db_connection()
                try:
                    cursor = conn.cursor()
                    new_tag = target_queue.strip()
                    placeholders = ','.join('?' * len(batch))
                    cursor.execute(
                        f'SELECT id, tags, filled_by_torrent_id, filled_by_magnet FROM media_items WHERE id IN ({placeholders})',
                        batch
                    )
                    rows = cursor.fetchall()
                    now = datetime.now()
                    pushed_hashes = []
                    import re as _re_assign
                    for row in rows:
                        existing_tags = [t.strip() for t in (row['tags'] or '').split(',') if t.strip()]
                        if new_tag in existing_tags:
                            continue
                        existing_tags.append(new_tag)
                        updated_tags = ','.join(existing_tags)
                        cursor.execute(
                            'UPDATE media_items SET tags = ?, last_updated = ? WHERE id = ?',
                            [updated_tags, now, row['id']]
                        )
                        total_processed += 1

                        # Resolve this row's cli_mount info_hash (same convention used
                        # elsewhere: nzb: prefix stripped, or infohash parsed from magnet)
                        info_hash = ''
                        torrent_id = str(row['filled_by_torrent_id'] or '')
                        if torrent_id.startswith('nzb:'):
                            info_hash = torrent_id[4:]
                        else:
                            magnet = row['filled_by_magnet'] or ''
                            m = _re_assign.search(r'urn:btih:([0-9a-fA-F]{40})', magnet, _re_assign.IGNORECASE)
                            if m:
                                info_hash = m.group(1).lower()
                        if info_hash:
                            pushed_hashes.append((info_hash, updated_tags, row['id']))
                    conn.commit()

                    if pushed_hashes:
                        try:
                            from usenet.climount_client import get_climount_client as _get_dc_assign
                            _dc_assign = _get_dc_assign()
                            if _dc_assign and _dc_assign.is_enabled():
                                for _ih, _tags, _row_id in pushed_hashes:
                                    if _dc_assign.push_tags(_ih, _tags):
                                        logging.info(f"[AssignTags] Pushed tags '{_tags}' to cli_mount for {_ih}")
                                        cursor.execute(
                                            'UPDATE media_items SET tags_pushed_at = ? WHERE id = ?',
                                            [datetime.now(), _row_id]
                                        )
                                        conn.commit()
                                    else:
                                        logging.warning(f"[AssignTags] cli_mount tag push returned false for {_ih} (tags='{_tags}')")
                            else:
                                logging.warning('[AssignTags] cli_mount client disabled/not configured — tags not pushed')
                        except Exception as _push_err:
                            logging.warning(f'[AssignTags] cli_mount tag push error: {_push_err}')
                    else:
                        logging.info('[AssignTags] No pushable info_hash resolved for any updated row — nothing sent to cli_mount')
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e):
                        logging.error("Database is locked during bulk assign_tags.")
                        conn.rollback()
                        return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
                    else:
                        error_count += 1
                        conn.rollback()
                        errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                        logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
                except Exception as e:
                    error_count += 1
                    conn.rollback()
                    errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                    logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
                finally:
                    conn.close()
            elif action == 'update_tags' and target_queue:  # target_queue contains the tag; replaces all existing tags
                logging.info("Entering 'update_tags' block.")
                conn = get_db_connection()
                try:
                    cursor = conn.cursor()
                    new_tag = target_queue.strip()
                    placeholders = ','.join('?' * len(batch))
                    cursor.execute(
                        f'SELECT id, tags, filled_by_torrent_id, filled_by_magnet FROM media_items WHERE id IN ({placeholders})',
                        batch
                    )
                    rows = cursor.fetchall()
                    now = datetime.now()
                    pushed_hashes = []
                    import re as _re_update
                    for row in rows:
                        cursor.execute(
                            'UPDATE media_items SET tags = ?, last_updated = ? WHERE id = ?',
                            [new_tag, now, row['id']]
                        )
                        total_processed += 1

                        info_hash = ''
                        torrent_id = str(row['filled_by_torrent_id'] or '')
                        if torrent_id.startswith('nzb:'):
                            info_hash = torrent_id[4:]
                        else:
                            magnet = row['filled_by_magnet'] or ''
                            m = _re_update.search(r'urn:btih:([0-9a-fA-F]{40})', magnet, _re_update.IGNORECASE)
                            if m:
                                info_hash = m.group(1).lower()
                        if info_hash:
                            pushed_hashes.append((info_hash, new_tag, row['id']))
                    conn.commit()

                    if pushed_hashes:
                        try:
                            from usenet.climount_client import get_climount_client as _get_dc_update
                            _dc_update = _get_dc_update()
                            if _dc_update and _dc_update.is_enabled():
                                for _ih, _tags, _row_id in pushed_hashes:
                                    if _dc_update.push_tags(_ih, _tags):
                                        logging.info(f"[UpdateTags] Pushed tags '{_tags}' to cli_mount for {_ih}")
                                        cursor.execute(
                                            'UPDATE media_items SET tags_pushed_at = ? WHERE id = ?',
                                            [datetime.now(), _row_id]
                                        )
                                        conn.commit()
                                    else:
                                        logging.warning(f"[UpdateTags] cli_mount tag push returned false for {_ih} (tags='{_tags}')")
                            else:
                                logging.warning('[UpdateTags] cli_mount client disabled/not configured — tags not pushed')
                        except Exception as _push_err:
                            logging.warning(f'[UpdateTags] cli_mount tag push error: {_push_err}')
                    else:
                        logging.info('[UpdateTags] No pushable info_hash resolved for any updated row — nothing sent to cli_mount')
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e):
                        logging.error("Database is locked during bulk update_tags.")
                        conn.rollback()
                        return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
                    else:
                        error_count += 1
                        conn.rollback()
                        errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                        logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
                except Exception as e:
                    error_count += 1
                    conn.rollback()
                    errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                    logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
                finally:
                    conn.close()
            elif action == 'remove_tags' and target_queue:  # target_queue contains the tag to remove
                logging.info("Entering 'remove_tags' block.")
                conn = get_db_connection()
                try:
                    cursor = conn.cursor()
                    tag_to_remove = target_queue.strip()
                    placeholders = ','.join('?' * len(batch))
                    cursor.execute(
                        f'SELECT id, tags, filled_by_torrent_id, filled_by_magnet FROM media_items WHERE id IN ({placeholders})',
                        batch
                    )
                    rows = cursor.fetchall()
                    now = datetime.now()
                    pushed_hashes = []
                    import re as _re_remove
                    for row in rows:
                        existing_tags = [t.strip() for t in (row['tags'] or '').split(',') if t.strip()]
                        if tag_to_remove not in existing_tags:
                            continue
                        remaining_tags = [t for t in existing_tags if t != tag_to_remove]
                        updated_tags = ','.join(remaining_tags)
                        cursor.execute(
                            'UPDATE media_items SET tags = ?, last_updated = ? WHERE id = ?',
                            [updated_tags, now, row['id']]
                        )
                        total_processed += 1

                        info_hash = ''
                        torrent_id = str(row['filled_by_torrent_id'] or '')
                        if torrent_id.startswith('nzb:'):
                            info_hash = torrent_id[4:]
                        else:
                            magnet = row['filled_by_magnet'] or ''
                            m = _re_remove.search(r'urn:btih:([0-9a-fA-F]{40})', magnet, _re_remove.IGNORECASE)
                            if m:
                                info_hash = m.group(1).lower()
                        if info_hash:
                            pushed_hashes.append((info_hash, tag_to_remove, row['id']))
                    conn.commit()

                    if pushed_hashes:
                        try:
                            from usenet.climount_client import get_climount_client as _get_dc_remove
                            _dc_remove = _get_dc_remove()
                            if _dc_remove and _dc_remove.is_enabled():
                                for _ih, _tag, _row_id in pushed_hashes:
                                    if _dc_remove.remove_tags(_ih, _tag):
                                        logging.info(f"[RemoveTags] Removed tag '{_tag}' from cli_mount for {_ih}")
                                        cursor.execute(
                                            'UPDATE media_items SET tags_pushed_at = ? WHERE id = ?',
                                            [datetime.now(), _row_id]
                                        )
                                        conn.commit()
                                    else:
                                        logging.warning(f"[RemoveTags] cli_mount tag removal returned false for {_ih} (tag='{_tag}')")
                            else:
                                logging.warning('[RemoveTags] cli_mount client disabled/not configured — tag not removed remotely')
                        except Exception as _push_err:
                            logging.warning(f'[RemoveTags] cli_mount tag removal error: {_push_err}')
                    else:
                        logging.info('[RemoveTags] No pushable info_hash resolved for any updated row — nothing sent to cli_mount')
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e):
                        logging.error("Database is locked during bulk remove_tags.")
                        conn.rollback()
                        return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
                    else:
                        error_count += 1
                        conn.rollback()
                        errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                        logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
                except Exception as e:
                    error_count += 1
                    conn.rollback()
                    errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                    logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
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
                        errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                        logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
                except Exception as e:
                    error_count += 1
                    conn.rollback()
                    errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                    logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
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
                               filled_by_file, title, type, episode_title, version, original_scraped_torrent_title,
                               filled_by_torrent_id, filled_by_magnet
                        FROM media_items
                        WHERE id IN ({placeholders_select})
                    """
                    cursor_rescape_batch.execute(query_select, batch)
                    db_columns = [column[0] for column in cursor_rescape_batch.description]
                    items_in_batch_details_raw = [dict(zip(db_columns, row)) for row in cursor_rescape_batch.fetchall()]
                
                except Exception as e:
                    logging.error(f"Error fetching batch details for rescrape: {str(e)}", exc_info=True)
                    errors.append(f"Error fetching details for batch {i//batch_size + 1}: {str(e)}")
                    if conn_rescape_batch: conn_rescape_batch.close()
                    continue # Skip to the next BATCH_SIZE chunk of selected_items
                
                prepared_items_for_db_update = [] 

                for item_db_data in items_in_batch_details_raw: 
                    item_id = item_db_data['id']
                    try:
                        logging.info(f"Rescrape: Processing item_id: {item_id} for file/Plex ops. Current state: {item_db_data.get('state')}, Version: {item_db_data.get('version')}")

                        # --- Start: File Deletion & Plex Removal Logic (using item_db_data) ---
                        if item_db_data['state'] in ['Collected', 'Upgrading']:
                            queue_plex_removal_for_item(item_db_data, file_management, item_id)

                        if item_db_data['state'] in ['Collected', 'Upgrading'] and \
                           (item_db_data.get('location_on_disk') or item_db_data.get('original_path_for_symlink')):
                            sleep(0.5)
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

                # --- Remove cli_mount entries for torrents/NZBs no longer needed ---
                # Collect unique torrent_ids from items being rescrapped that are in active states
                batch_id_set = set(int(x) for x in batch)
                torrent_ids_to_check = {}  # torrent_id -> infohash (or '' for NZBs using torrent_id directly)
                for item_db_data in items_in_batch_details_raw:
                    if item_db_data.get('state') not in ('Collected', 'Upgrading', 'Checking'):
                        continue
                    tid = item_db_data.get('filled_by_torrent_id') or ''
                    if not tid:
                        continue
                    if tid not in torrent_ids_to_check:
                        magnet = item_db_data.get('filled_by_magnet') or ''
                        import re as _re
                        m = _re.search(r'urn:btih:([0-9a-fA-F]{40})', magnet, _re.IGNORECASE)
                        torrent_ids_to_check[tid] = m.group(1).lower() if m else ''

                if torrent_ids_to_check:
                    try:
                        from usenet.climount_client import get_climount_client as _get_dc
                        _dc = _get_dc()
                        if _dc and _dc.is_enabled():
                            tid_placeholders = ','.join('?' * len(torrent_ids_to_check))
                            # Items outside this batch that still actively use these torrent IDs
                            survivors = cursor_rescape_batch.execute(
                                f"""SELECT filled_by_torrent_id FROM media_items
                                    WHERE filled_by_torrent_id IN ({tid_placeholders})
                                    AND state IN ('Collected','Upgrading','Checking')
                                    AND id NOT IN ({','.join('?' * len(batch_id_set))})""",
                                list(torrent_ids_to_check.keys()) + list(batch_id_set)
                            ).fetchall()
                            still_used = {r[0] for r in survivors}
                            for tid, infohash in torrent_ids_to_check.items():
                                if tid in still_used:
                                    logging.info(f"Rescrape: Skipping cli_mount removal for {tid!r} — still used by other items")
                                    continue
                                if tid.startswith('nzb:'):
                                    nzb_hash = tid[4:]
                                    if nzb_hash:
                                        _dc.remove_nzb(nzb_hash)
                                        logging.info(f"Rescrape: Removed NZB {nzb_hash!r} from cli_mount")
                                elif infohash:
                                    _dc.remove_nzb(infohash)
                                    logging.info(f"Rescrape: Removed debrid torrent {infohash!r} (RD id={tid!r}) from cli_mount")
                                else:
                                    logging.warning(f"Rescrape: Cannot remove {tid!r} from cli_mount — no infohash in magnet link")
                    except Exception as _cm_err:
                        logging.warning(f"Rescrape: cli_mount removal failed: {_cm_err}")
                # --- End cli_mount removal ---

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
                                   {RESET_COLLECTION_STATE_SQL},
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
                            logging.info(f"Rescrape: Successfully committed DB update for {rows_affected_by_update} items for batch {i//batch_size + 1}.")
                        else:
                            conn_rescape_batch.rollback()
                            mismatch_error_msg = f"Rescrape DB Update: Expected to affect {len(item_ids_for_update_clause)} items, but DB reported {rows_affected_by_update}. Rolled back changes for this group of items in batch {i//batch_size + 1}."
                            logging.error(mismatch_error_msg)
                            errors.append(mismatch_error_msg)
                            error_count += len(item_ids_for_update_clause) 
                    
                    except sqlite3.OperationalError as e_db_update:
                        if "database is locked" in str(e_db_update):
                            logging.error(f"Database is locked during bulk rescrape update for batch {i//batch_size + 1}.")
                            if conn_rescape_batch: conn_rescape_batch.rollback()
                            # Specific error response for database locked
                            return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
                        else:
                            if conn_rescape_batch: 
                                try:
                                    conn_rescape_batch.rollback() 
                                except Exception as e_rollback:
                                    logging.error(f"Rescrape: Error during rollback attempt: {e_rollback}", exc_info=True)

                            db_update_err_msg = f"Error during batch database update for rescrape (batch {i//batch_size + 1}): {str(e_db_update)}"
                            errors.append(db_update_err_msg)
                            logging.error(f"Rescrape: {db_update_err_msg}", exc_info=True)
                            error_count += len(prepared_items_for_db_update)
                    except Exception as e_db_update: 
                        if conn_rescape_batch: 
                            try:
                                conn_rescape_batch.rollback() 
                            except Exception as e_rollback:
                                logging.error(f"Rescrape: Error during rollback attempt: {e_rollback}", exc_info=True)

                        db_update_err_msg = f"Error during batch database update for rescrape (batch {i//batch_size + 1}): {str(e_db_update)}"
                        errors.append(db_update_err_msg)
                        logging.error(f"Rescrape: {db_update_err_msg}", exc_info=True)
                        error_count += len(prepared_items_for_db_update) 
                
                elif items_in_batch_details_raw: 
                    logging.info(f"Rescrape: No items from batch {i//batch_size + 1} were successfully prepared for database update (e.g., all had file/Plex processing errors).")

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
                        errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                        logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
                except Exception as e:
                    error_count += 1
                    conn.rollback()
                    errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                    logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
                finally:
                    conn.close()
            elif action == 'ghostlist':
                logging.info("Entering 'ghostlist' block.")
                conn = get_db_connection()
                try:
                    cursor = conn.cursor()
                    placeholders = ','.join('?' * len(batch))
                    cursor.execute(
                        f'UPDATE media_items SET ghostlisted = TRUE, state = ?, last_updated = ? WHERE id IN ({placeholders})',
                        ['Blacklisted', datetime.now()] + batch
                    )
                    total_processed += cursor.rowcount
                    conn.commit()
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e):
                        logging.error("Database is locked during bulk ghostlist.")
                        conn.rollback()
                        return jsonify({'success': False, 'error': 'database is locked', 'database_locked': True}), 503
                    else:
                        error_count += 1
                        conn.rollback()
                        errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                        logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
                except Exception as e:
                    error_count += 1
                    conn.rollback()
                    errors.append(f"Error in batch {i//batch_size + 1}: {str(e)}")
                    logging.error(f"Error in batch {i//batch_size + 1}: {str(e)}")
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
            elif action == 'verify_symlinks':
                logging.info("Entering 'verify_symlinks' block.")
                for item_id in batch:
                    try:
                        item = get_media_item_by_id(item_id)
                        if not item:
                            error_count += 1
                            errors.append(f"Item {item_id} not found")
                            continue

                        # Check if item has a symlink path
                        symlink_path = item.get('location_on_disk')
                        if not symlink_path:
                            error_count += 1
                            errors.append(f"Item {item_id}: No symlink path found")
                            continue

                        # Check if the symlink file exists
                        if not os.path.exists(symlink_path):
                            error_count += 1
                            errors.append(f"Item {item_id}: Symlink file does not exist at {symlink_path}")
                            continue

                        # Add to verification queue
                        success = add_symlinked_file_for_verification(item_id, symlink_path)
                        if success:
                            total_processed += 1
                            logging.info(f"Added item {item_id} to symlink verification queue")
                        else:
                            error_count += 1
                            errors.append(f"Item {item_id}: Failed to add to verification queue")
                    except sqlite3.OperationalError as e:
                        if "database is locked" in str(e):
                            logging.error("Database is locked during bulk verify_symlinks.")
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
                "assign_tags": f"tagged with '{target_queue}'",
                "update_tags": f"tags set to '{target_queue}'",
                "remove_tags": f"tag '{target_queue}' removed",
                "early_release": "marked as early release and moved to Wanted queue",
                "rescrape": "deleted files/Plex entries for and moved to Wanted queue", # Added rescrape message
                "force_priority": "marked for forced priority",
                "ghostlist": "ghostlisted and moved to Blacklisted queue",
                "resync": "resynchronized",
                "verify_symlinks": "added to symlink verification queue"
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

def rescrape_single_item(item_id):
    """Reset a single media_items row back to Wanted, cleaning up its file/Plex/
    cli_mount references first. Single-item counterpart to bulk_queue_action's
    'rescrape' action (see that function for the batched/CASE-SQL version) —
    kept in sync with the same side effects and safety checks:
      - Plex/symlink removal is only queued for Collected/Upgrading items
      - cli_mount torrent/NZB removal is skipped if another item still uses
        the same filled_by_torrent_id in an active state
      - version's '*' (primary-version marker) is stripped on rescrape

    Returns {'success': True} on success, or {'success': False, 'error': str}.
    Does not raise for expected failure modes (item not found, db locked).
    """
    from database import get_db_connection

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            """SELECT id, state, location_on_disk, original_path_for_symlink,
                      filled_by_file, title, type, episode_title, version,
                      original_scraped_torrent_title, filled_by_torrent_id, filled_by_magnet
               FROM media_items WHERE id = ?""",
            (item_id,)
        )
        row = cursor.fetchone()
        if not row:
            return {'success': False, 'error': f'Item {item_id} not found'}
        columns = [c[0] for c in cursor.description]
        item_db_data = dict(zip(columns, row))

        file_management = get_setting('File Management', 'file_collection_management', 'Plex')

        if item_db_data['state'] in ('Collected', 'Upgrading'):
            queue_plex_removal_for_item(item_db_data, file_management, item_id)
            if item_db_data.get('location_on_disk') or item_db_data.get('original_path_for_symlink'):
                sleep(0.5)

        current_version_val = item_db_data.get('version')
        cleaned_version_val = current_version_val
        if isinstance(current_version_val, str) and '*' in current_version_val:
            cleaned_version_val = current_version_val.replace('*', '')

        # cli_mount torrent/NZB removal — skip if another item still actively uses it
        if item_db_data['state'] in ('Collected', 'Upgrading', 'Checking'):
            tid = item_db_data.get('filled_by_torrent_id') or ''
            if tid:
                try:
                    from usenet.climount_client import get_climount_client as _get_dc
                    _dc = _get_dc()
                    if _dc and _dc.is_enabled():
                        still_used = cursor.execute(
                            """SELECT 1 FROM media_items
                               WHERE filled_by_torrent_id = ? AND state IN ('Collected','Upgrading','Checking')
                               AND id != ?""",
                            (tid, item_id)
                        ).fetchone()
                        if still_used:
                            logging.info(f"Rescrape: Skipping cli_mount removal for {tid!r} — still used by another item")
                        elif tid.startswith('nzb:'):
                            nzb_hash = tid[4:]
                            if nzb_hash:
                                _dc.remove_nzb(nzb_hash)
                                logging.info(f"Rescrape: Removed NZB {nzb_hash!r} from cli_mount")
                        else:
                            magnet = item_db_data.get('filled_by_magnet') or ''
                            import re as _re
                            m = _re.search(r'urn:btih:([0-9a-fA-F]{40})', magnet, _re.IGNORECASE)
                            infohash = m.group(1).lower() if m else ''
                            if infohash:
                                _dc.remove_nzb(infohash)
                                logging.info(f"Rescrape: Removed debrid torrent {infohash!r} (RD id={tid!r}) from cli_mount")
                            else:
                                logging.warning(f"Rescrape: Cannot remove {tid!r} from cli_mount — no infohash in magnet link")
                except Exception as cm_err:
                    logging.warning(f"Rescrape: cli_mount removal failed for item {item_id}: {cm_err}")

        cursor.execute(
            f"""UPDATE media_items
               SET state = 'Wanted',
                   location_on_disk = NULL,
                   original_path_for_symlink = NULL,
                   filled_by_file = NULL,
                   filled_by_title = NULL,
                   filled_by_magnet = NULL,
                   filled_by_torrent_id = NULL,
                   {RESET_COLLECTION_STATE_SQL},
                   rescrape_original_torrent_title = ?,
                   original_scraped_torrent_title = NULL,
                   upgrading_from = NULL,
                   upgrading = NULL,
                   version = ?,
                   fall_back_to_single_scraper = 0,
                   last_updated = ?
               WHERE id = ?""",
            (item_db_data.get('original_scraped_torrent_title'), cleaned_version_val, datetime.now(), item_id)
        )

        if cursor.rowcount != 1:
            conn.rollback()
            return {'success': False, 'error': f'Expected to update 1 row for item {item_id}, DB reported {cursor.rowcount}'}

        conn.commit()
        logging.info(f"Rescrape: Successfully reset item {item_id} to Wanted.")
        return {'success': True}

    except sqlite3.OperationalError as e:
        if conn:
            conn.rollback()
        if "database is locked" in str(e):
            logging.error(f"Database is locked during rescrape of item {item_id}.")
            return {'success': False, 'error': 'database is locked', 'database_locked': True}
        logging.error(f"Operational error rescraping item {item_id}: {e}", exc_info=True)
        return {'success': False, 'error': str(e)}
    except Exception as e:
        if conn:
            conn.rollback()
        logging.error(f"Error rescraping item {item_id}: {e}", exc_info=True)
        return {'success': False, 'error': str(e)}
    finally:
        if conn:
            conn.close()


def _find_rescrape_candidates(ms_item_id, item_type, season_number=None, episode_number=None, filename=None):
    """Resolve an external client's (ms_item_id, type[, season/episode], filename)
    tuple down to matching media_items rows. ms_item_id alone cannot uniquely
    identify a single version/file (see column comment in schema_management.py —
    it's shared across every version of a movie, and for episodes it's the
    *show's* ratingKey/ItemId, not the episode's), so this narrows by
    ms_item_id + type [+ season/episode for episodes], then disambiguates by
    filename (matched against location_basename, falling back to filled_by_file)
    when more than one candidate remains.

    Returns a list of dicts (each: id, title, version, state, filename/basename)
    — empty if no match, one item if unambiguous, multiple if the caller needs
    to prompt the user (e.g. multiple versions with no filename to disambiguate,
    or filename didn't narrow it down).
    """
    from database import get_db_connection

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        query = """SELECT id, title, type, version, state, season_number, episode_number,
                          location_basename, filled_by_file
                   FROM media_items
                   WHERE ms_item_id = ? AND type = ?"""
        params = [ms_item_id, item_type]
        if item_type == 'episode':
            query += " AND season_number = ? AND episode_number = ?"
            params += [season_number, episode_number]
        cursor.execute(query, params)
        columns = [c[0] for c in cursor.description]
        rows = [dict(zip(columns, r)) for r in cursor.fetchall()]

        if len(rows) <= 1 or not filename:
            return rows

        wanted_basename = os.path.basename(filename)
        by_location = [r for r in rows if r.get('location_basename') == wanted_basename]
        if by_location:
            return by_location

        by_filled = [r for r in rows if r.get('filled_by_file') and os.path.basename(r['filled_by_file']) == wanted_basename]
        if by_filled:
            return by_filled

        return rows
    finally:
        conn.close()


@database_bp.route('/rescrape_item', methods=['POST'])
@user_required
def rescrape_item():
    """Move a single Collected/Upgrading item back to Wanted for re-request,
    for use by external clients (e.g. the Plezy companion app) that only know
    a media-server item id (Plex ratingKey / Jellyfin item id) and, optionally,
    the on-disk filename of the specific version they want re-requested.

    Request JSON:
      item_id: int            — optional; if given, skips lookup and rescrapes
                                 this media_items row directly (e.g. the row the
                                 client already resolved via a version picker).
      ms_item_id: str          — Plex ratingKey or Jellyfin item id.
      type: 'movie'|'episode'
      season_number, episode_number: int — required when type == 'episode'.
      filename: str            — basename of the specific file/version to
                                  disambiguate when ms_item_id matches more
                                  than one version row.

    Either item_id, or (ms_item_id + type [+ season/episode]), is required.

    Response:
      200 {"success": true} on success.
      200 {"success": false, "error": "ambiguous", "candidates": [...]} when
          more than one version matches and filename didn't disambiguate —
          the client should prompt the user and retry with item_id set.
      404 {"success": false, "error": "..."} when nothing matches.
      400 {"success": false, "error": "..."} on a malformed request.
    """
    data = request.get_json(silent=True) or {}

    item_id = data.get('item_id')
    if item_id:
        try:
            item_id = int(item_id)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'item_id must be an integer'}), 400
        result = rescrape_single_item(item_id)
        status = 200 if result.get('success') else (503 if result.get('database_locked') else 400)
        return jsonify(result), status

    ms_item_id = data.get('ms_item_id')
    item_type = data.get('type')
    if not ms_item_id or item_type not in ('movie', 'episode'):
        return jsonify({'success': False, 'error': 'ms_item_id and type (movie|episode) are required when item_id is not provided'}), 400

    season_number = data.get('season_number')
    episode_number = data.get('episode_number')
    if item_type == 'episode' and (season_number is None or episode_number is None):
        return jsonify({'success': False, 'error': 'season_number and episode_number are required when type is episode'}), 400

    filename = data.get('filename')

    candidates = _find_rescrape_candidates(ms_item_id, item_type, season_number, episode_number, filename)

    if not candidates:
        return jsonify({'success': False, 'error': 'No matching item found for the given ms_item_id'}), 404

    if len(candidates) > 1:
        return jsonify({
            'success': False,
            'error': 'ambiguous',
            'message': 'Multiple versions match — retry with item_id set to the chosen candidate',
            'candidates': candidates,
        }), 200

    result = rescrape_single_item(candidates[0]['id'])
    status = 200 if result.get('success') else (503 if result.get('database_locked') else 400)
    return jsonify(result), status


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
            # Check if we're in limited environment mode
            from utilities.set_supervisor_env import is_limited_environment
            limited_env = is_limited_environment()
            
            if file_management == 'Plex':
                if mounted_location and item.get('location_on_disk'):
                    if not limited_env:
                        try:
                            if os.path.exists(item['location_on_disk']):
                                os.remove(item['location_on_disk'])
                                logging.info(f"Delete item: Removed file from disk {item['location_on_disk']} (Plex mode).")
                        except Exception as e:
                            logging.error(f"Error deleting file at {item['location_on_disk']}: {str(e)}")
                    else:
                        logging.info(f"Delete item: Skipped file deletion for {item['location_on_disk']} due to limited environment mode (Plex mode).")

                # Allow time for file system operations to complete
                sleep(1)

                # Immediate Plex removal
                path_to_remove_from_plex = item.get('filled_by_file')
                if path_to_remove_from_plex:
                    try:
                        logging.info(f"Delete item: Attempting immediate Plex removal for {item['title']} ({path_to_remove_from_plex}).")
                        plex_removal_result = remove_file_from_plex(item['title'], path_to_remove_from_plex, item.get('episode_title'))
                        if plex_removal_result:
                            logging.info(f"Delete item: Successfully removed {item['title']} from Plex ({path_to_remove_from_plex}).")
                        else:
                            # Direct removal failed - try scan & empty trash as fallback
                            logging.warning(f"Delete item: Direct Plex removal failed for {item['title']}. Trying scan & empty trash...")
                            try:
                                # Determine section type based on item type
                                section_type = 'movie' if item.get('type') == 'movie' else 'show'
                                scan_paths = [os.path.dirname(path_to_remove_from_plex)] if path_to_remove_from_plex else None
                                scan_and_empty_plex_trash(paths=scan_paths, section_type=section_type)
                                logging.info(f"Delete item: Triggered library scan and trash empty for {item['title']} (section_type={section_type}).")
                            except Exception as scan_err:
                                logging.warning(f"Delete item: Scan & empty trash also failed for {item['title']}: {scan_err}. Item may need manual removal from Plex.")
                    except Exception as e:
                        logging.error(f"Delete item: Error during immediate Plex removal for {item['title']} ({path_to_remove_from_plex}): {str(e)}.")
                else:
                    logging.warning(f"Delete item: No 'filled_by_file' path for item {item_id} ({item['title']}). Skipping Plex removal.")

            elif file_management == 'Symlinked/Local':
                symlink_path_from_db = item.get('location_on_disk')
                original_file_path_to_remove_disk = item.get('original_path_for_symlink')

                # Translate the Plex path to local path for symlink deletion
                # location_on_disk may contain Plex's view of the path (e.g., /movies/...)
                # but the actual symlink is at the local path (e.g., /mnt/symlinked/movies/...)
                symlink_path_to_remove_disk = translate_plex_path_to_local(symlink_path_from_db) if symlink_path_from_db else None

                if symlink_path_to_remove_disk and symlink_path_to_remove_disk != symlink_path_from_db:
                    logging.info(f"Delete item: Translated path '{symlink_path_from_db}' to local path '{symlink_path_to_remove_disk}'")

                # Determine the path Plex uses (keep the original Plex path for API call)
                path_for_plex_api_call = symlink_path_from_db

                if symlink_path_to_remove_disk:
                    # Always remove symlinks (they're just pointers)
                    try:
                        if os.path.exists(symlink_path_to_remove_disk) and os.path.islink(symlink_path_to_remove_disk):
                            os.unlink(symlink_path_to_remove_disk)
                            logging.info(f"Delete item: Removed symlink {symlink_path_to_remove_disk} (Symlinked/Local mode).")
                        elif os.path.islink(symlink_path_to_remove_disk):
                            # Broken symlink - still remove it
                            os.unlink(symlink_path_to_remove_disk)
                            logging.info(f"Delete item: Removed broken symlink {symlink_path_to_remove_disk} (Symlinked/Local mode).")
                        else:
                            logging.warning(f"Delete item: Path {symlink_path_to_remove_disk} is not a symlink or doesn't exist. Skipping symlink removal.")
                    except Exception as e:
                        logging.error(f"Error removing symlink at {symlink_path_to_remove_disk}: {str(e)}")
                
                if original_file_path_to_remove_disk:
                    if not path_for_plex_api_call: # Fallback if symlink path wasn't set
                        path_for_plex_api_call = original_file_path_to_remove_disk
                    # Only delete original files if not in limited environment mode
                    if not limited_env:
                        try:
                            if os.path.exists(original_file_path_to_remove_disk):
                                os.remove(original_file_path_to_remove_disk)
                                logging.info(f"Delete item: Removed original file {original_file_path_to_remove_disk} (Symlinked/Local mode).")
                        except Exception as e:
                            logging.error(f"Error deleting original file at {original_file_path_to_remove_disk}: {str(e)}")
                    else:
                        logging.info(f"Delete item: Skipped original file deletion for {original_file_path_to_remove_disk} due to limited environment mode (Symlinked/Local mode).")

                # Allow time for file system operations to complete
                sleep(1)

                # Immediate Plex removal using the determined path
                if path_for_plex_api_call:
                    try:
                        logging.info(f"Delete item: Attempting immediate Plex removal for {item['title']} using path {path_for_plex_api_call} (Symlinked/Local mode).")
                        plex_removal_result = remove_file_from_plex(item['title'], path_for_plex_api_call, item.get('episode_title'))
                        if plex_removal_result:
                            logging.info(f"Delete item: Successfully removed {item['title']} from Plex ({path_for_plex_api_call}).")
                        else:
                            # Direct removal failed - try scan & empty trash as fallback
                            logging.warning(f"Delete item: Direct Plex removal failed for {item['title']}. Trying scan & empty trash...")
                            try:
                                # Determine section type based on item type
                                section_type = 'movie' if item.get('type') == 'movie' else 'show'
                                scan_paths = [os.path.dirname(path_for_plex_api_call)] if path_for_plex_api_call else None
                                scan_and_empty_plex_trash(paths=scan_paths, section_type=section_type)
                                logging.info(f"Delete item: Triggered library scan and trash empty for {item['title']} (section_type={section_type}).")
                            except Exception as scan_err:
                                logging.warning(f"Delete item: Scan & empty trash also failed for {item['title']}: {scan_err}. Item may need manual removal from Plex.")
                    except Exception as e:
                        logging.error(f"Delete item: Error during immediate Plex removal for {item['title']} ({path_for_plex_api_call}): {str(e)}.")
                else:
                    logging.warning(f"Delete item: No suitable path found for Plex removal for item {item_id} ({item['title']}) (Symlinked/Local mode). Skipping Plex removal.")

        # --- cli_mount removal ---
        # Remove the entry from cli_mount (and RD) if no other live items share the same torrent.
        # Must happen BEFORE DB deletion so we can still check siblings.
        _torrent_id = item.get('filled_by_torrent_id') or ''
        _magnet = item.get('filled_by_magnet') or ''
        if _torrent_id and item.get('state') in ('Collected', 'Upgrading', 'Checking'):
            try:
                import re as _re_dh
                from database import get_db_connection as _gdb_del
                _conn_del = _gdb_del()
                try:
                    _sibs = _conn_del.execute(
                        "SELECT COUNT(*) FROM media_items "
                        "WHERE filled_by_torrent_id = ? AND state IN ('Collected','Upgrading','Checking') AND id != ?",
                        (_torrent_id, item_id)
                    ).fetchone()[0]
                finally:
                    _conn_del.close()
                if _sibs == 0:
                    from usenet.climount_client import get_climount_client as _get_dc_del
                    _dc_del = _get_dc_del()
                    if _dc_del and _dc_del.is_enabled():
                        if _torrent_id.startswith('nzb:'):
                            _nzb_hash = _torrent_id[4:]
                            if _nzb_hash:
                                _dc_del.remove_nzb(_nzb_hash)
                                logging.info(f"Delete item: Removed NZB {_nzb_hash!r} from cli_mount for item {item_id}")
                        else:
                            _m = _re_dh.search(r'urn:btih:([0-9a-fA-F]{40})', _magnet, _re_dh.IGNORECASE)
                            _infohash = _m.group(1).lower() if _m else ''
                            if _infohash:
                                _dc_del.remove_nzb(_infohash)
                                logging.info(f"Delete item: Removed debrid torrent {_infohash!r} from cli_mount for item {item_id}")
                else:
                    logging.info(f"Delete item: Skipping cli_mount removal for {_torrent_id!r} — {_sibs} sibling(s) still active")
            except Exception as _cm_del_err:
                logging.warning(f"Delete item: cli_mount removal failed for item {item_id}: {_cm_del_err}")
        # --- End cli_mount removal ---

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
            parsed_version = parse_filename_for_version(item['filled_by_file'], is_nzb=str(item.get('filled_by_torrent_id') or '').startswith('nzb:'))
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
            parsed_version = parse_filename_for_version(item['filled_by_file'], is_nzb=str(item.get('filled_by_torrent_id') or '').startswith('nzb:'))
            
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
            enabled=True,
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

@database_bp.route('/get_column_values/<column_name>', methods=['GET'])
@admin_required
def get_column_values(column_name):
    """Lazy-load distinct values for a specific column.

    This endpoint returns distinct values for filter dropdowns on-demand,
    instead of loading all columns upfront. Improves initial page load performance.
    """
    conn = None
    try:
        from database import get_db_connection
        conn = get_db_connection()
        cursor = conn.cursor()

        # Validate column name to prevent SQL injection
        cursor.execute("PRAGMA table_info(media_items)")
        valid_columns = [row[1] for row in cursor.fetchall()]

        if column_name not in valid_columns:
            return jsonify({'success': False, 'error': f'Invalid column name: "{column_name}". Please select a valid column from the available options.'}), 400

        values = []

        # Special handling for different column types
        if column_name == 'content_source':
            cursor.execute(f"SELECT DISTINCT \"{column_name}\" FROM media_items WHERE \"{column_name}\" IS NOT NULL")
            distinct_source_ids = [row[0] for row in cursor.fetchall()]
            values = distinct_source_ids

        elif column_name in ('state', 'type'):
            cursor.execute(f"SELECT DISTINCT \"{column_name}\" FROM media_items ORDER BY \"{column_name}\"")
            values = [row[0] if row[0] is not None else "None" for row in cursor.fetchall()]
            # Add special state options
            if column_name == 'state':
                values.append('ghostlisted')
                values.append('all_blacklisted')

        elif column_name == 'version':
            cursor.execute(f"SELECT DISTINCT \"{column_name}\" FROM media_items")
            db_versions_raw = [row[0] for row in cursor.fetchall()]
            version_list = []
            has_none = False
            for v in db_versions_raw:
                if v is None or v == "":
                    has_none = True
                    continue
                version_list.append(str(v))
            if has_none:
                version_list.append("None")
            values = sorted(list(set(version_list)))

        else:
            # Generic handling for other columns
            cursor.execute(f"SELECT DISTINCT \"{column_name}\" FROM media_items WHERE \"{column_name}\" IS NOT NULL ORDER BY \"{column_name}\"")
            values = [row[0] for row in cursor.fetchall()]

        return jsonify({'success': True, 'column': column_name, 'values': values})

    except sqlite3.OperationalError as e:
        logging.error(f"SQLite operational error fetching column values for '{column_name}': {str(e)}")
        if "database is locked" in str(e).lower():
            error_msg = f"The database is busy. Please try loading values for '{column_name}' again in a moment."
            status_code = 503
        else:
            error_msg = f"Failed to load filter values for column '{column_name}'. Please try again."
            status_code = 500
        return jsonify({'success': False, 'error': error_msg, 'database_locked': 'locked' in str(e).lower()}), status_code
    except sqlite3.Error as e:
        logging.error(f"SQLite error fetching column values for '{column_name}': {str(e)}")
        return jsonify({'success': False, 'error': f"Database error while loading '{column_name}' values. Please refresh the page and try again."}), 500
    except Exception as e:
        logging.error(f"Unexpected error fetching column values for '{column_name}': {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': f"An unexpected error occurred while loading '{column_name}' values. Please try again."}), 500
    finally:
        if conn:
            conn.close()
