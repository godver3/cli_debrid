from flask import jsonify, request, render_template, session, flash, Blueprint, current_app
import sqlite3
import string
from database import get_db_connection
import logging
from routes import admin_required
import os
from database import create_tables, verify_database, bulk_delete_by_imdb_id
from sqlalchemy import text, inspect
from extensions import db
from database import remove_from_media_items

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


        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({
                'table': render_template('database_table.html', 
                                        items=items, 
                                        all_columns=all_columns,
                                        selected_columns=selected_columns,
                                        content_type=content_type),
                'pagination': render_template('database_pagination.html',
                                            alphabet=alphabet,
                                            current_letter=current_letter,
                                            content_type=content_type,
                                            filter_column=filter_column,
                                            filter_value=filter_value,
                                            sort_column=sort_column,
                                            sort_order=sort_order)
            })
        
    except sqlite3.Error as e:
        logging.error(f"SQLite error in database route: {str(e)}")
        items = []
        flash(f"Database error: {str(e)}", "error")
    except Exception as e:
        logging.error(f"Unexpected error in database route: {str(e)}")
        items = []
        flash("An unexpected error occurred. Please try again later.", "error")

    return render_template('database.html', 
                           items=items, 
                           all_columns=all_columns,
                           selected_columns=selected_columns,
                           filter_column=filter_column,
                           filter_value=filter_value,
                           sort_column=sort_column,
                           sort_order=sort_order,
                           alphabet=alphabet,
                           current_letter=current_letter,
                           content_type=content_type)
    
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