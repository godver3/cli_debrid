"""Debug page, bulk delete, release-date refresh, battery cache reset, database delete, queue contents."""

from flask import jsonify, request, redirect, url_for
import logging
from routes import admin_required
import os
import glob
from ._common import debug_bp

@debug_bp.route('/debug_functions')
@admin_required
def debug_functions():
    # The Debug page was split into Tools, Maintenance and Diagnostics.
    return redirect(url_for('tool_pages.tools'))

@debug_bp.route('/bulk_delete_by_imdb', methods=['POST'])
@admin_required
def bulk_delete_by_imdb():
    id_value = request.form.get('imdb_id')
    if not id_value:
        return jsonify({'success': False, 'error': 'ID is required'})

    id_type = 'imdb_id' if id_value.startswith('tt') else 'tmdb_id'
    from database import bulk_delete_by_id
    deleted_count = bulk_delete_by_id(id_value, id_type)
    
    if deleted_count > 0:
        return jsonify({'success': True, 'message': f'Successfully deleted {deleted_count} items with {id_type.upper()}: {id_value}'})
    else:
        return jsonify({'success': False, 'error': f'No items found with {id_type.upper()}: {id_value}'})

@debug_bp.route('/refresh_release_dates', methods=['POST'])
@admin_required
def refresh_release_dates_route():
    from metadata.metadata import refresh_release_dates # Added import here
    # Manual trigger: bypass the periodic task's 24h-per-item cache floor so
    # this always forces a real provider re-fetch, not just a re-read of
    # whatever was already cached (which could be minutes old and still
    # showing 'Unknown').
    refresh_release_dates(force_bypass_cache=True)
    return jsonify({'success': True, 'message': 'Release dates refreshed successfully'})

@debug_bp.route('/reset_battery_show_cache', methods=['POST'])
@admin_required
def reset_battery_show_cache():
    """Reset battery show cache by nulling last_trakt_fetch for all shows.
    This forces a fresh re-fetch from TVDB/Trakt on the next TV status update,
    which will correctly apply the canceled vs ended cross-check fix.
    """
    try:
        from cli_battery.app.database import managed_session, Item
        with managed_session() as session:
            updated = session.query(Item).filter(Item.type == 'show').update(
                {'last_trakt_fetch': None},
                synchronize_session=False
            )
        logging.info(f"[Reset Battery Cache] Nulled last_trakt_fetch for {updated} shows in battery DB")
        return jsonify({
            'success': True,
            'message': f'Reset cache for {updated} shows. Run "Update TV Show Status" task to re-fetch status from TVDB/Trakt.'
        })
    except Exception as e:
        logging.error(f"reset_battery_show_cache error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@debug_bp.route('/delete_database', methods=['POST'])
@admin_required
def delete_database():
    try:
        confirm = request.form.get('confirm_delete', '')
        retain_blacklist = request.form.get('retain_blacklist') == 'on'
        
        if confirm != 'DELETE':
            return jsonify({'success': False, 'error': 'Please type DELETE to confirm database deletion'})
        
        from database import get_db_connection
        conn = get_db_connection()
        cursor = conn.cursor()
        
        if retain_blacklist:
            logging.info("Retaining blacklisted items while deleting database")
            # Get blacklisted items first
            cursor.execute("""
                SELECT * FROM media_items 
                WHERE blacklisted_date IS NOT NULL
            """)
            blacklisted_items = cursor.fetchall()
            
            # Delete all tables except media_items and sqlite_sequence
            cursor.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' 
                AND name NOT IN ('media_items', 'sqlite_sequence')
            """)
            tables = cursor.fetchall()
            for table in tables:
                cursor.execute(f"DROP TABLE IF EXISTS {table['name']}")
            
            # Delete non-blacklisted items from media_items
            cursor.execute("""
                DELETE FROM media_items 
                WHERE blacklisted_date IS NULL
            """)
            
            logging.info(f"Retained {len(blacklisted_items)} blacklisted items")
        else:
            logging.info("Deleting entire database including blacklisted items")
            # Delete all tables except sqlite_sequence
            cursor.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' 
                AND name != 'sqlite_sequence'
            """)
            tables = cursor.fetchall()
            for table in tables:
                cursor.execute(f"DROP TABLE IF EXISTS {table['name']}")
        
        conn.commit()
        conn.close()
        
        # Recreate all necessary tables
        from database.schema_management import verify_database
        verify_database()  # This will recreate all tables including torrent_additions
        
        # Recreate battery database tables
        try:
            from cli_battery.app.database import init_db
            init_db()
            logging.info("Successfully recreated battery database tables")
        except Exception as e:
            logging.error(f"Error recreating battery database: {str(e)}")
        
        # Delete cache files and not wanted files
        db_content_dir = os.environ['USER_DB_CONTENT']
        
        # Delete cache files and not wanted files
        cache_files = glob.glob(os.path.join(db_content_dir, '*cache*.pkl'))
        not_wanted_files = ['not_wanted_magnets.pkl', 'not_wanted_urls.pkl']
        rclone_progress_file = 'rclone_to_symlink_processed_files.json' # Define the file name
        deleted_files = []

        # Delete cache files
        for cache_file in cache_files:
            try:
                os.remove(cache_file)
                deleted_files.append(os.path.basename(cache_file))
                logging.info(f"Deleted cache file: {cache_file}")
            except Exception as e:
                logging.warning(f"Failed to delete cache file {cache_file}: {str(e)}")
        
        # Delete not wanted files
        for not_wanted_file in not_wanted_files:
            file_path = os.path.join(db_content_dir, not_wanted_file)
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    deleted_files.append(not_wanted_file)
                    logging.info(f"Deleted not wanted file: {file_path}")
                except Exception as e:
                    logging.warning(f"Failed to delete not wanted file {file_path}: {str(e)}")
        
        # Delete Rclone progress file
        rclone_progress_file_path = os.path.join(db_content_dir, rclone_progress_file)
        if os.path.exists(rclone_progress_file_path):
            try:
                os.remove(rclone_progress_file_path)
                deleted_files.append(rclone_progress_file)
                logging.info(f"Deleted Rclone progress file: {rclone_progress_file_path}")
            except Exception as e:
                logging.warning(f"Failed to delete Rclone progress file {rclone_progress_file_path}: {str(e)}")
        
        message = 'Database deleted successfully'
        if retain_blacklist:
            message += f' (retained {len(blacklisted_items)} blacklisted items)'
        if deleted_files:
            message += f' and removed {len(deleted_files)} files: {", ".join(deleted_files)}'
        
        return jsonify({'success': True, 'message': message})
        
    except Exception as e:
        logging.error(f"Error deleting database: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})

@debug_bp.route('/api/bulk_queue_contents', methods=['GET'])
def get_queue_contents():
    from database import get_db_connection
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, title, state, type, season_number, episode_number, year
            FROM media_items
            WHERE state IN ('Adding', 'Blacklisted', 'Checking', 'Scraping', 'Sleeping', 'Unreleased', 'Wanted', 'Pending Uncached', 'Upgrading')
        ''')
        items = cursor.fetchall()

        queue_contents = {
            'Adding': [], 'Blacklisted': [], 'Checking': [], 'Scraping': [],
            'Sleeping': [], 'Unreleased': [], 'Wanted': [], 'Pending Uncached': [], 'Upgrading': []
        }
        
        for item in items:
            item_dict = dict(item)
            # Ensure title is a string, defaulting if None from DB
            base_title = item_dict.get('title', "Unknown Title")

            if item_dict['type'] == 'episode':
                s_num = item_dict.get('season_number')
                e_num = item_dict.get('episode_number')
                
                display_title = base_title
                
                if s_num is not None and e_num is not None:
                    display_title = f"{base_title} S{s_num:02d}E{e_num:02d}"
                elif s_num is not None: # Only season
                    display_title = f"{base_title} S{s_num:02d}"
                elif e_num is not None: # Only episode
                    display_title = f"{base_title} E{e_num:02d}"
                # If both s_num and e_num are None, display_title remains base_title
                
                item_dict['title'] = display_title

            elif item_dict['type'] == 'movie':
                year = item_dict.get('year')
                if year is not None:
                    item_dict['title'] = f"{base_title} ({year})"
                else:
                    item_dict['title'] = base_title # Just the title if year is None
            
            # The SQL query filters by states that are keys in queue_contents,
            # so direct assignment should be safe.
            queue_contents[item_dict['state']].append(item_dict)
        
        return jsonify(queue_contents)
    except Exception as e:
        logging.error(f"Error fetching queue contents: {str(e)}")
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()
