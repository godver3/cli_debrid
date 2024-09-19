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

database_bp = Blueprint('database', __name__)

@database_bp.route('/', methods=['GET', 'POST'])
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

    if not action or not selected_items:
        return jsonify({'success': False, 'error': 'Action and selected items are required'})

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if action == 'delete':
            cursor.execute('DELETE FROM media_items WHERE id IN ({})'.format(','.join('?' * len(selected_items))), selected_items)
            message = f"Successfully deleted {cursor.rowcount} items"
        elif action == 'move' and target_queue:
            cursor.execute('UPDATE media_items SET state = ? WHERE id IN ({})'.format(','.join('?' * len(selected_items))), [target_queue] + selected_items)
            message = f"Successfully moved {cursor.rowcount} items to {target_queue} queue"
        else:
            return jsonify({'success': False, 'error': 'Invalid action or missing target queue'})

        conn.commit()
        return jsonify({'success': True, 'message': message})
    except Exception as e:
        conn.rollback()
        logging.error(f"Error performing bulk action: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})
    finally:
        conn.close()

@database_bp.route('/delete_item', methods=['POST'])
def delete_item():
    data = request.json
    item_id = data.get('item_id')
    
    if not item_id:
        return jsonify({'success': False, 'error': 'No item ID provided'}), 400

    try:
        remove_from_media_items(item_id)
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Error deleting item: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

def perform_database_migration():
    logging.info("Performing database migration...")
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