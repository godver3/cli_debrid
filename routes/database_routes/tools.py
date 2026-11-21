"""Reverse parser, watch history, PhalanxDB, visual browser and column values."""

from flask import jsonify, request, render_template, flash
import sqlite3
import logging
from utilities.settings import get_setting
from utilities.reverse_parser import get_version_settings, get_default_version, get_version_order, parse_filename_for_version
from ..models import admin_required
import os
from datetime import datetime
from utilities.phalanx_db_cache_manager import PhalanxDBClassManager
from utilities.web_scraper import get_media_meta
from ._common import database_bp, source_sql

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

        if column_name == 'source':
            # Computed column: 'Debrid' (any provider), each provider seen, Usenet, None.
            expr = source_sql(valid_columns)
            found = [row[0] for row in cursor.execute(f"SELECT DISTINCT {expr} FROM media_items")] if expr else []
            values = ['Usenet'] if 'Usenet' in found else []
            debrid = sorted(v for v in found if v and v.startswith('Debrid'))
            if debrid:
                values += ['Debrid'] + [v for v in debrid if v != 'Debrid']
            if '' in found:
                values.append('None')
            return jsonify({'success': True, 'column': column_name, 'values': values})

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
