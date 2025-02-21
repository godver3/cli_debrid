from flask import jsonify, request, render_template, session, flash, Blueprint, current_app
import sqlite3
import string
from database import get_db_connection, get_all_media_items, update_media_item_state
import logging
from sqlalchemy import text, inspect
from extensions import db
from database import remove_from_media_items
from settings import get_setting
import json
from reverse_parser import get_version_settings, get_default_version, get_version_order, parse_filename_for_version
from .models import admin_required
from utilities.plex_functions import remove_file_from_plex
from database.database_reading import get_media_item_by_id
import os
from datetime import datetime
from time import sleep
database_bp = Blueprint('database', __name__)

@database_bp.route('/', methods=['GET', 'POST'])
@admin_required
def index():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Get all column names
        cursor.execute("PRAGMA table_info(media_items)")
        all_columns = [column[1] for column in cursor.fetchall()]

        # Define the default columns
        default_columns = [
            'imdb_id', 'title', 'year', 'release_date', 'state', 'type',
            'season_number', 'episode_number', 'collected_at', 'version'
        ]

        # Get or set selected columns
        if request.method == 'POST':
            selected_columns = request.form.getlist('columns')
            session['selected_columns'] = selected_columns
        else:
            selected_columns = session.get('selected_columns')

        # If no columns are selected, use the default columns
        if not selected_columns:
            selected_columns = [col for col in default_columns if col in all_columns]
            if not selected_columns:
                selected_columns = ['id']  # Fallback to ID if none of the default columns exist

        # Ensure at least one column is selected
        if not selected_columns:
            selected_columns = ['id']

        # Get filter and sort parameters
        filter_column = request.args.get('filter_column', '')
        filter_value = request.args.get('filter_value', '')
        sort_column = request.args.get('sort_column', 'id')  # Default sort by id
        sort_order = request.args.get('sort_order', 'asc')
        content_type = request.args.get('content_type', 'movie')  # Default to 'movie'
        current_letter = request.args.get('letter', 'A')

        # Validate sort_column
        if sort_column not in all_columns:
            sort_column = 'id'  # Fallback to 'id' if invalid column is provided

        # Validate sort_order
        if sort_order.lower() not in ['asc', 'desc']:
            sort_order = 'asc'  # Fallback to 'asc' if invalid order is provided

        # Define alphabet here
        alphabet = list(string.ascii_uppercase)

        # Construct the SQL query
        query = f"SELECT {', '.join(selected_columns)} FROM media_items"
        where_clauses = []
        params = []

        # Apply custom filter if present, otherwise apply content type and letter filters
        if filter_column and filter_value:
            where_clauses.append(f"{filter_column} LIKE ?")
            params.append(f"%{filter_value}%")
            # Reset content_type and current_letter when custom filter is applied
            content_type = 'all'
            current_letter = ''
        else:
            if content_type != 'all':
                where_clauses.append("type = ?")
                params.append(content_type)
            
            if current_letter:
                if current_letter == '#':
                    where_clauses.append("title LIKE '0%' OR title LIKE '1%' OR title LIKE '2%' OR title LIKE '3%' OR title LIKE '4%' OR title LIKE '5%' OR title LIKE '6%' OR title LIKE '7%' OR title LIKE '8%' OR title LIKE '9%' OR title LIKE '[%' OR title LIKE '(%' OR title LIKE '{%'")
                elif current_letter.isalpha():
                    where_clauses.append("title LIKE ?")
                    params.append(f"{current_letter}%")

        # Construct the ORDER BY clause safely
        order_clause = f"ORDER BY {sort_column} {sort_order}"

        # Ensure 'id' is always included in the query, even if not displayed
        query_columns = list(set(selected_columns + ['id']))
        
        # Construct the final query
        query = f"SELECT {', '.join(query_columns)} FROM media_items"
        if where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)
        query += f" {order_clause}"

        # Log the query and parameters for debugging
        logging.debug(f"Executing query: {query}")
        logging.debug(f"Query parameters: {params}")

        # Execute the query
        cursor.execute(query, params)
        items = cursor.fetchall()

        # Log the number of items fetched
        logging.debug(f"Fetched {len(items)} items from the database")

        conn.close()

        # Convert items to a list of dictionaries, always including 'id'
        items = [dict(zip(query_columns, item)) for item in items]

        # Prepare the data dictionary
        data = {
            'items': items,
            'all_columns': all_columns,
            'selected_columns': selected_columns,
            'filter_column': filter_column,
            'filter_value': filter_value,
            'sort_column': sort_column,
            'sort_order': sort_order,
            'alphabet': alphabet,
            'current_letter': current_letter,
            'content_type': content_type
        }

        if request.args.get('ajax') == '1':
            return jsonify(data)
        else:
            return render_template('database.html', **data)
        
    except sqlite3.Error as e:
        logging.error(f"SQLite error in database route: {str(e)}")
        error_message = f"Database error: {str(e)}"
    except Exception as e:
        logging.error(f"Unexpected error in database route: {str(e)}")
        error_message = "An unexpected error occurred. Please try again later."

    if request.args.get('ajax') == '1':
        return jsonify({'error': error_message}), 500
    else:
        flash(error_message, "error")
        # Remove 'items' from the arguments here
        return render_template('database.html', **{**data, 'items': []})

@database_bp.route('/bulk_queue_action', methods=['POST'])
def bulk_queue_action():
    action = request.form.get('action')
    target_queue = request.form.get('target_queue')
    selected_items = request.form.getlist('selected_items')
    blacklist = request.form.get('blacklist', 'false').lower() == 'true'

    if not action or not selected_items:
        return jsonify({'success': False, 'error': 'Action and selected items are required'})

    # Process items in batches to avoid SQLite parameter limits
    BATCH_SIZE = 450  # Stay well under SQLite's 999 parameter limit
    total_processed = 0
    error_count = 0
    errors = []
    
    try:
        for i in range(0, len(selected_items), BATCH_SIZE):
            batch = selected_items[i:i + BATCH_SIZE]
            
            if action == 'delete':
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
            else:
                return jsonify({'success': False, 'error': 'Invalid action or missing target queue'})

        if error_count > 0:
            message = f"Completed with {error_count} errors. Successfully processed {total_processed} items."
            if errors:
                message += f" First few errors: {'; '.join(errors[:3])}"
            return jsonify({'success': True, 'message': message, 'warning': True})
        else:
            action_text = "deleted" if action == "delete" else "moved to {target_queue} queue" if action == "move" else "marked as early release and moved to Wanted queue" if action == "early_release" else f"changed to version {target_queue}"
            message = f"Successfully {action_text} {total_processed} items"
            return jsonify({'success': True, 'message': message})

    except Exception as e:
        logging.error(f"Error performing bulk action: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})

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
            update_media_item_state(item_id, 'Blacklisted')
        else:
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
        items = get_all_media_items()
        updated_count = 0
        for item in items:
            if item['filled_by_file']:
                parsed_version = parse_filename_for_version(item['filled_by_file'])
                
                # Only update if the parsed version is different from the current version
                current_version = item['version'] if 'version' in item.keys() else None
                if parsed_version != current_version:
                    try:
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