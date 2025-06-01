from flask import jsonify, Blueprint, render_template, request, redirect, url_for, flash, current_app, Response, stream_with_context
from flask.json import jsonify
from queues.initialization import get_all_wanted_from_enabled_sources
from queues.run_program import (
    get_and_add_recent_collected_from_plex, 
    get_and_add_all_collected_from_plex, 
    ProgramRunner, 
    run_local_library_scan, 
    run_recent_local_library_scan
)
from database.manual_blacklist import add_to_manual_blacklist, remove_from_manual_blacklist, get_manual_blacklist, save_manual_blacklist
from utilities.settings import get_all_settings, get_setting, set_setting
from queues.config_manager import load_config
import logging
from routes import admin_required
from cli_battery.app.direct_api import DirectAPI
from database.torrent_tracking import get_recent_additions, get_torrent_history
import os
import glob
from routes.api_tracker import api 
import time
from datetime import datetime
from routes.notifications import send_notifications, get_enabled_notifications
import requests
from datetime import datetime, timedelta
from queues.queue_manager import QueueManager
from database.not_wanted_magnets import (
    get_not_wanted_magnets, get_not_wanted_urls,
    purge_not_wanted_magnets_file, save_not_wanted_magnets,
    load_not_wanted_urls, save_not_wanted_urls
)
import json
from debrid import get_debrid_provider
import threading
import queue
import asyncio
from utilities.plex_functions import get_collected_from_plex, plex_update_item
from content_checkers.content_cache_management import (
    load_source_cache, save_source_cache, 
    should_process_item, update_cache_for_item
)
import traceback
from database.symlink_verification import get_unverified_files, get_verification_stats
from content_checkers.content_source_detail import append_content_source_detail
# Import necessary modules for symlink recovery
import re
from pathlib import Path
from utilities.settings import get_setting
from datetime import datetime
# Import reverse parser
from utilities.reverse_parser import parse_filename_for_version
# Imports for streaming
import threading
import time
import json
from flask import Response, stream_with_context
# Import Plex debug functions
# Import sqlite3 for error handling and add_media_item
import sqlite3
from utilities.local_library_scan import convert_item_to_symlink, get_symlink_path, create_symlink, resync_symlinks_with_new_settings
from scraper.functions.ptt_parser import parse_with_ptt
from database.database_writing import add_media_item
from routes.program_operation_routes import get_program_runner
from utilities.plex_removal_cache import cache_plex_removal

debug_bp = Blueprint('debug', __name__)

# Global progress tracking
scan_progress = {}

# Global dictionary to store analysis progress
analysis_progress = {}

# Global dictionary for Rclone to Symlink progress tracking
rclone_scan_progress = {}

# Global dictionary for Riven symlink analysis progress
riven_analysis_progress = {}

# --- Helper function to get cache files ---
def get_cache_files():
    """Returns a list of content source cache filenames."""
    try:
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        if not os.path.isdir(db_content_dir):
            logging.error(f"Cache directory not found: {db_content_dir}")
            return []
        
        # Find files matching the pattern
        pattern = os.path.join(db_content_dir, 'content_source_*.pkl')
        cache_files = [os.path.basename(f) for f in glob.glob(pattern)]
        return sorted(cache_files)
    except Exception as e:
        logging.error(f"Error getting cache files: {str(e)}")
        return []
# --- End Helper function ---

def async_get_wanted_content(source):
    results = {}
    try:
        if source == 'all':
            # Get all enabled sources
            content_sources = get_all_settings().get('Content Sources', {})
            enabled_sources = {source_id: data for source_id, data in content_sources.items() if data.get('enabled', False)}
            
            total_added = 0
            total_processed = 0
            total_cache_skipped = 0
            total_media_type_skipped = 0
            all_errors = []

            for source_id in enabled_sources:
                logging.info(f"Processing source {source_id} as part of 'all'...")
                result = get_and_add_wanted_content(source_id)
                total_added += result.get('added', 0)
                total_processed += result.get('processed', 0)
                total_cache_skipped += result.get('cache_skipped', 0)
                total_media_type_skipped += result.get('media_type_skipped', 0)
                if result.get('error'):
                    all_errors.append(f"{source_id}: {result['error']}")
            
            message_parts = [f"All Sources: Added {total_added} items"]
            if total_processed > 0: message_parts.append(f"Processed {total_processed}")
            if total_cache_skipped > 0: message_parts.append(f"Skipped {total_cache_skipped} (cache)")
            if total_media_type_skipped > 0: message_parts.append(f"Skipped {total_media_type_skipped} (media type)")
            message = ", ".join(message_parts) + "."
            
            results = {'success': True, 'message': message}
            if all_errors:
                results['error'] = "Errors: " + "; ".join(all_errors)
                results['success'] = False # Mark as failure if any source had errors

        else:
            # Get the display name for the single content source
            content_sources = get_all_settings().get('Content Sources', {})
            source_config = content_sources.get(source, {})
            if isinstance(source_config, dict) and source_config.get('display_name'):
                display_name = source_config['display_name']
            else:
                display_name = ' '.join(word.capitalize() for word in source.split('_'))
            
            # Process the single source
            result = get_and_add_wanted_content(source)
            
            added = result.get('added', 0)
            processed = result.get('processed', 0)
            cache_skipped = result.get('cache_skipped', 0)
            media_type_skipped = result.get('media_type_skipped', 0)
            error = result.get('error')
            
            message_parts = [f"{display_name}: Added {added} items"]
            if processed > 0: message_parts.append(f"Processed {processed}")
            if cache_skipped > 0: message_parts.append(f"Skipped {cache_skipped} (cache)")
            if media_type_skipped > 0: message_parts.append(f"Skipped {media_type_skipped} (media type)")
            message = ", ".join(message_parts) + "."

            results = {'success': error is None, 'message': message}
            if error:
                results['error'] = str(error)
        
        return results
    except Exception as e:
        logging.error(f"Error in async_get_wanted_content for source '{source}': {e}", exc_info=True)
        return {'success': False, 'error': f"Unexpected error processing source {source}: {str(e)}"}

def async_get_collected_from_plex(collection_type):
    try:
        if collection_type == 'all':
            if get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
                logging.info("Full library scan disabled for now")
                #run_local_library_scan()
            else:
                get_and_add_all_collected_from_plex()
            message = 'Successfully retrieved and added all collected items from Library'
        elif collection_type == 'recent':
            if get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
                logging.info("Recent library scan disabled for now")
                #run_recent_local_library_scan()
            else:
                get_and_add_recent_collected_from_plex()
            message = 'Successfully retrieved and added recent collected items from Library'
        else:
            raise ValueError('Invalid collection type')
        
        return {'success': True, 'message': message}
    except Exception as e:
        return {'success': False, 'error': str(e)}

@debug_bp.route('/debug_functions')
@admin_required
def debug_functions():
    content_sources = get_all_settings().get('Content Sources', {})
    enabled_sources = {source: data for source, data in content_sources.items() if data.get('enabled', False)}
    cache_files = get_cache_files() # Fetch cache files
    return render_template(
        'debug_functions.html', 
        content_sources=enabled_sources,
        cache_files=cache_files # Pass cache files to template
    )

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
    refresh_release_dates()
    return jsonify({'success': True, 'message': 'Release dates refreshed successfully'})

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

def move_item_to_queue(item_id, target_queue):
    from database import get_db_connection
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('UPDATE media_items SET state = ? WHERE id = ?', (target_queue, item_id))
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

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

@debug_bp.route('/manual_blacklist', methods=['GET', 'POST'])
@admin_required
def manual_blacklist():
    from metadata.metadata import get_tmdb_id_and_media_type # Import the function to determine media type

    if request.method == 'POST':
        action = request.form.get('action')
        imdb_id = request.form.get('imdb_id')
        
        if not imdb_id:
            flash('IMDb ID is required', 'error')
            return redirect(url_for('debug.manual_blacklist'))

        blacklist = get_manual_blacklist()
        direct_api = DirectAPI()
        
        if action == 'add':
            try:
                logging.info(f"Attempting to add IMDb ID '{imdb_id}' to manual blacklist.")
                # 1. Determine the actual media type
                tmdb_id, actual_media_type = get_tmdb_id_and_media_type(imdb_id)

                if not actual_media_type:
                    flash(f'Could not determine media type for IMDb ID {imdb_id}. Cannot add to blacklist.', 'error')
                    return redirect(url_for('debug.manual_blacklist'))

                # 2. Fetch metadata based on the determined type
                metadata = None
                if actual_media_type == 'tv':
                    metadata_tuple = direct_api.get_show_metadata(imdb_id)
                    if metadata_tuple: metadata = metadata_tuple[0]
                elif actual_media_type == 'movie':
                    metadata_tuple = direct_api.get_movie_metadata(imdb_id)
                    if metadata_tuple: metadata = metadata_tuple[0]

                # Ensure metadata is a dictionary if found
                if metadata and isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except json.JSONDecodeError:
                        flash(f'Failed to parse metadata for {imdb_id}. Cannot add to blacklist.', 'error')
                        metadata = None

                if not metadata or not isinstance(metadata, dict):
                    flash(f'Unable to fetch metadata for IMDb ID {imdb_id} (Type: {actual_media_type}). Cannot add to blacklist.', 'error')
                    return redirect(url_for('debug.manual_blacklist'))

                # 3. Determine the media type to store in the blacklist file
                media_type_to_store = 'episode' if actual_media_type == 'tv' else 'movie'

                # 4. Add to blacklist with the intended (potentially 'episode') media type
                logging.info(f"Calling add_to_manual_blacklist with: imdb_id='{imdb_id}', media_type='{media_type_to_store}', title='{metadata.get('title', 'Unknown Title')}', year='{str(metadata.get('year', ''))}'")
                add_to_manual_blacklist(
                    imdb_id=imdb_id,
                    media_type=media_type_to_store,
                    title=metadata.get('title', 'Unknown Title'),
                    year=str(metadata.get('year', '')),
                )
                flash(f'Successfully added {metadata.get("title", "Item")} ({actual_media_type}) to blacklist as type "{media_type_to_store}"', 'success')

            except Exception as e:
                flash(f'Error adding to blacklist: {str(e)}', 'error')
                logging.error(f"Error adding to blacklist: {str(e)}", exc_info=True)

        elif action == 'update_seasons':
            try:
                if imdb_id in blacklist:
                    item = blacklist[imdb_id]
                    # REVERT: Check against 'episode' type here
                    if item['media_type'] == 'episode':
                        all_seasons = request.form.get('all_seasons') == 'on'

                        if all_seasons:
                            item['seasons'] = []
                        else:
                            selected_seasons = request.form.getlist('seasons')
                            item['seasons'] = sorted([int(s) for s in selected_seasons if s.isdigit()])

                        save_manual_blacklist(blacklist)
                        return jsonify({'success': True, 'message': 'Successfully updated seasons'})
                    else:
                        # This branch should technically not be hit for TV shows if 'add' stores them as 'episode'
                        return jsonify({'success': False, 'error': 'Only items stored as type "episode" can have seasons updated'}), 400
                else:
                    return jsonify({'success': False, 'error': 'Item not found in blacklist'}), 404
            except Exception as e:
                logging.error(f"Error updating seasons via AJAX: {str(e)}", exc_info=True)
                return jsonify({'success': False, 'error': str(e)}), 500

        elif action == 'remove':
            try:
                remove_from_manual_blacklist(imdb_id)
                flash('Successfully removed from blacklist', 'success')
            except Exception as e:
                flash(f'Error removing from blacklist: {str(e)}', 'error')

        if action != 'update_seasons':
             return redirect(url_for('debug.manual_blacklist'))

    # --- GET Request Logic ---
    blacklist = get_manual_blacklist()
    
    # ... (keep existing sorting logic) ...
    def get_sort_key(item):
        try:
            title = item[1].get('title', '')
            if not isinstance(title, str):
                logging.warning(f"Invalid title type for IMDb ID {item[0]}: {type(title)}")
                title = ''
            return title.lower()
        except Exception as e:
            logging.error(f"Error getting sort key for blacklist item {item[0]}: {str(e)}")
            return ''
    sorted_blacklist = dict(sorted(blacklist.items(), key=get_sort_key))
    
    direct_api = DirectAPI()
    for imdb_id, item in sorted_blacklist.items():
        # REVERT: Check against 'episode' type for fetching seasons
        if item['media_type'] == 'episode':
            try:
                # Fetching seasons based on IMDb ID remains the same
                seasons_data, _ = direct_api.get_show_seasons(imdb_id)
                if seasons_data:
                    logging.debug(f"Seasons data for {imdb_id}: {seasons_data}")
                    if isinstance(seasons_data, str):
                        seasons_data = json.loads(seasons_data)

                    if isinstance(seasons_data, dict) and all(str(k).isdigit() for k in seasons_data.keys()):
                        item['available_seasons'] = sorted([int(season) for season in seasons_data.keys()])
                        item['season_episodes'] = {int(season): data.get('episode_count', 0) for season, data in seasons_data.items()}
                    else: # Backward compatibility
                        item['available_seasons'] = sorted([int(s['season_number']) for s in seasons_data.get('seasons', []) if str(s.get('season_number')).isdigit()])
                        item['season_episodes'] = {}
                else:
                    item['available_seasons'] = []
                    item['season_episodes'] = {}
            except Exception as e:
                logging.error(f"Error fetching seasons for {imdb_id}: {str(e)}")
                item['available_seasons'] = []
                item['season_episodes'] = {}

    return render_template('manual_blacklist.html', blacklist=sorted_blacklist)

@debug_bp.route('/api/get_collected_from_plex', methods=['POST'])
@admin_required
def get_collected_from_plex():
    collection_type = request.json.get('collection_type', 'recent')
    
    if collection_type not in ['all', 'recent']:
        return jsonify({'success': False, 'error': 'Invalid collection type'}), 400

    from routes.extensions import task_queue

    task_id = task_queue.add_task(async_get_collected_from_plex, collection_type)
    return jsonify({'task_id': task_id}), 202

@debug_bp.route('/api/direct_plex_scan', methods=['POST'])
@admin_required
def direct_plex_scan():
    """Direct route to scan Plex library with progress tracking."""
    try:
        import uuid
        from utilities.plex_functions import get_collected_from_plex
        
        # Generate unique scan ID
        scan_id = str(uuid.uuid4())
        scan_progress[scan_id] = {
            'status': 'starting',
            'message': 'Initializing scan...',
            'movies_count': 0,
            'episodes_count': 0,
            'complete': False,
            'shows_processed': 0,
            'total_shows': 0,
            'movies_processed': 0,
            'total_movies': 0,
            'episodes_found': 0,
            'errors': []
        }
        
        def progress_callback(status_type, message, counts=None):
            """Callback function to update progress"""
            logging.debug(f"Scan {scan_id}: progress_callback called with status={status_type}, message='{message}', counts={counts}") # Add logging
            update = {
                'status': status_type,
                'message': message,
                'complete': status_type in ['complete', 'error']
            }
            if counts:
                update.update(counts)
            scan_progress[scan_id].update(update)
            logging.debug(f"Scan {scan_id}: scan_progress updated to: {scan_progress[scan_id]}") # Add logging

            if status_type == 'error':
                scan_progress[scan_id]['errors'].append(message)
        
        def run_scan():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                
                # Update status to show collection starting
                scan_progress[scan_id].update({
                    'status': 'collecting',
                    'message': 'Starting Plex library scan...',
                    'phase': 'collection'
                })
                
                # Run the scan with progress callback
                collected_content = loop.run_until_complete(
                    get_collected_from_plex(
                        request='all',
                        progress_callback=progress_callback,
                        bypass=True
                    )
                )
                
                loop.close()
                
                if collected_content:
                    # Extract movies and episodes from collected content
                    movies = collected_content.get('movies', [])
                    episodes = collected_content.get('episodes', [])
                    
                    total_items = len(movies) + len(episodes)
                    logging.info(f"Retrieved {len(movies)} movies and {len(episodes)} episodes")
                    
                    # Update status to show database addition starting
                    scan_progress[scan_id].update({
                        'status': 'adding',
                        'message': f'Adding {total_items} items to database...',
                        'phase': 'database',
                        'total_items': total_items,
                        'processed_items': 0
                    })
                    
                    # Add the collected items to the database
                    from database.collected_items import add_collected_items
                    try:
                        if total_items > 0:
                            # Update progress before starting database addition
                            scan_progress[scan_id].update({
                                'status': 'adding',
                                'message': f'Adding {len(movies)} movies and {len(episodes)} episodes to database...',
                                'phase': 'database',
                                'complete': False  # Ensure we're not marked as complete during database phase
                            })
                            
                            add_collected_items(movies + episodes)
                            
                            # Only now mark as complete after database addition is done
                            scan_progress[scan_id].update({
                                'status': 'complete',
                                'message': f'Successfully scanned Plex library and added {len(movies)} movies and {len(episodes)} episodes to database',
                                'success': True,
                                'complete': True,
                                'phase': 'complete'
                            })
                        else:
                            scan_progress[scan_id].update({
                                'status': 'complete',
                                'message': 'Scanned Plex library but found no items to add',
                                'success': True,
                                'complete': True,
                                'phase': 'complete'
                            })
                    except Exception as e:
                        error_msg = f"Error adding collected items to database: {str(e)}"
                        logging.error(error_msg, exc_info=True)
                        scan_progress[scan_id].update({
                            'status': 'error',
                            'message': error_msg,
                            'success': False,
                            'complete': True,
                            'phase': 'error',
                            'errors': [error_msg]
                        })
                else:
                    scan_progress[scan_id].update({
                        'status': 'error',
                        'message': 'No content retrieved from Plex scan',
                        'success': False,
                        'complete': True,
                        'phase': 'error'
                    })
            except Exception as e:
                error_msg = str(e)
                logging.error(f"Error during Plex scan: {error_msg}", exc_info=True)
                # Ensure final counts are added even in case of error, if available
                final_counts = {
                   'shows_processed': scan_progress[scan_id].get('shows_processed', 0),
                   'total_shows': scan_progress[scan_id].get('total_shows', 0),
                   'movies_processed': scan_progress[scan_id].get('movies_processed', 0),
                   'total_movies': scan_progress[scan_id].get('total_movies', 0),
                   'episodes_found': scan_progress[scan_id].get('episodes_found', 0)
                }
                scan_progress[scan_id].update({
                    'status': 'error',
                    'message': f"Error during scan: {error_msg}",
                    'success': False,
                    'complete': True,
                    'phase': 'error',
                    'errors': [error_msg],
                    **final_counts # Also add counts on error
                })
            finally:
                # Ensure final counts are included in the final completion message if status wasn't error
                if scan_progress.get(scan_id) and scan_progress[scan_id].get('status') not in ['error', 'starting']:
                    final_counts = {
                        'shows_processed': scan_progress[scan_id].get('shows_processed', 0),
                        'total_shows': scan_progress[scan_id].get('total_shows', 0),
                        'movies_processed': scan_progress[scan_id].get('movies_processed', 0),
                        'total_movies': scan_progress[scan_id].get('total_movies', 0),
                        'episodes_found': scan_progress[scan_id].get('episodes_found', 0)
                    }
                    # Update the existing final status with the counts
                    scan_progress[scan_id].update(final_counts)
                    logging.info(f"Ensured final counts ({final_counts}) are in completion status for scan {scan_id}")

                # Clean up after 5 minutes
                threading.Timer(300, lambda: scan_progress.pop(scan_id, None)).start()
        
        # Start scan in background thread
        thread = threading.Thread(target=run_scan)
        thread.daemon = True
        thread.start()
        
        return jsonify({'success': True, 'scan_id': scan_id})
            
    except Exception as e:
        logging.error(f"Error initiating Plex scan: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)})

@debug_bp.route('/api/plex_scan_progress/<scan_id>')
def plex_scan_progress(scan_id):
    """SSE endpoint for tracking Plex scan progress."""
    def generate():
        logging.info(f"[Server SSE {scan_id}] Connection established.") # Added log
        while True:
            try: # Added try block for better error handling inside loop
                if scan_id not in scan_progress:
                    # Check if it's in analysis_progress as a fallback (might be analysis task)
                    if scan_id in analysis_progress:
                         progress = analysis_progress[scan_id]
                         yield f"data: {json.dumps(progress)}\n\n"
                         if progress.get('complete', False):
                             logging.info(f"[Server SSE {scan_id}] Analysis task complete=true detected.") # Added log
                             # Add a small delay AFTER sending the final message
                             logging.info(f"[Server SSE {scan_id}] Sleeping for 0.5s before break.") # Added log
                             time.sleep(0.5) # Sleep for 500ms 
                             logging.info(f"[Server SSE {scan_id}] Breaking loop after sleep.") # Added log
                             break # THEN break the loop
                    else:
                        logging.info(f"[Server SSE {scan_id}] Scan/Task ID not found.") # Added log
                        yield f"data: {json.dumps({'status': 'error', 'message': 'Scan or task not found'})}\n\n"
                        break
                    
                else: # Scan progress found
                    progress = scan_progress[scan_id]
                    logging.debug(f"[Server SSE {scan_id}] Current progress: {progress}") # Added debug log
                    
                    # Add error details to progress if any exist
                    if progress.get('errors'):
                        progress['error_details'] = progress['errors']
                    
                    data_to_send = json.dumps(progress)
                    logging.info(f"[Server SSE {scan_id}] Yielding data: {data_to_send[:200]}...") # Added log (truncated)
                    yield f"data: {data_to_send}\n\n"
                    
                    logging.debug(f"[Server SSE {scan_id}] Checking complete flag...") # Added debug log
                    if progress.get('complete', False): # Use .get() for safety
                        logging.info(f"[Server SSE {scan_id}] Scan task complete=true detected.") # Added log
                        # Add a small delay AFTER sending the final message
                        # to give the client time to process it before the connection closes.
                        logging.info(f"[Server SSE {scan_id}] Sleeping for 0.5s before break.") # Added log
                        time.sleep(0.5) # Sleep for 500ms 
                        logging.info(f"[Server SSE {scan_id}] Breaking loop after sleep.") # Added log
                        break # THEN break the loop
                    
                    logging.debug(f"[Server SSE {scan_id}] Sleeping for 1s before next iteration.") # Added debug log
                    time.sleep(1)
                    
                # except Exception as e: # Removed specific except block here to simplify
                #     logging.error(f"[Server SSE {scan_id}] Error inside generate loop: {e}", exc_info=True)
                #     try:
                #         yield f"data: {json.dumps({'status': 'error', 'message': f'Server error: {e}', 'complete': True})}\n\n"
                #     except Exception as yield_err:
                #         logging.error(f"[Server SSE {scan_id}] Failed to yield error message: {yield_err}")
                #     break # Exit loop on error
                
            except Exception as e:
                logging.error(f"[Server SSE {scan_id}] Error inside generate loop: {e}", exc_info=True)
                try:
                    yield f"data: {json.dumps({'status': 'error', 'message': f'Server error: {e}', 'complete': True})}\n\n"
                except Exception as yield_err:
                    logging.error(f"[Server SSE {scan_id}] Failed to yield error message: {yield_err}")
                break # Exit loop on error
        
        logging.info(f"[Server SSE {scan_id}] Generate loop finished.") # Added log
    
    return Response(stream_with_context(generate()), mimetype='text/event-stream')

@debug_bp.route('/api/task_status/<task_id>')
def task_status(task_id):
    from routes.extensions import task_queue

    task_info = task_queue.get_task_status(task_id)
    return jsonify(task_info)

def update_trakt_settings(content_sources):
    trakt_watchlist_enabled = any(
        source_data['enabled'] 
        for source_id, source_data in content_sources.items() 
        if source_id.startswith('Trakt Watchlist')
    )
    trakt_lists = ','.join([
        source_data.get('trakt_lists', '') 
        for source_id, source_data in content_sources.items()
        if source_id.startswith('Trakt Lists') and source_data['enabled']
    ])

    #set_setting('Trakt', 'user_watchlist_enabled', trakt_watchlist_enabled)
    #set_setting('Trakt', 'trakt_lists', trakt_lists)

def get_and_add_wanted_content(source_id):
    from content_checkers.overseerr import get_wanted_from_overseerr
    from content_checkers.collected import get_wanted_from_collected
    from content_checkers.plex_watchlist import get_wanted_from_plex_watchlist, get_wanted_from_other_plex_watchlist
    from content_checkers.plex_rss_watchlist import get_wanted_from_plex_rss, get_wanted_from_friends_plex_rss
    from content_checkers.trakt import get_wanted_from_trakt_lists, get_wanted_from_trakt_watchlist, get_wanted_from_trakt_collection, get_wanted_from_friend_trakt_watchlist, get_wanted_from_special_trakt_lists
    from content_checkers.mdb_list import get_wanted_from_mdblists
    from content_checkers.content_source_detail import append_content_source_detail
    from metadata.metadata import process_metadata

    content_sources = get_all_settings().get('Content Sources', {})
    source_data = content_sources.get(source_id) # Use .get for safety
    if not source_data:
        logging.error(f"Source ID {source_id} not found in settings.")
        return {'added': 0, 'processed': 0, 'cache_skipped': 0, 'media_type_skipped': 0, 'error': f"Source {source_id} not found"}

    source_type = source_id.split('_')[0]
    versions_from_config = source_data.get('versions', []) # Default to empty list if missing
    source_media_type = source_data.get('media_type', 'All')
    cutoff_date = source_data.get('cutoff_date', '')
    if cutoff_date:
        try:
            cutoff_date = datetime.strptime(cutoff_date, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            logging.warning(f"Invalid cutoff_date format in source {source_id}. Expected YYYY-MM-DD, got {cutoff_date}")
            cutoff_date = None

    logging.info(f"Processing source: {source_id}")
    logging.debug(f"Source type: {source_type}, media type: {source_media_type}, versions (as dict): {versions_from_config}")
    
    source_cache = load_source_cache(source_id)
    logging.debug(f"Initial cache state for {source_id}: {len(source_cache)} entries")
    cache_skipped = 0
    items_processed = 0
    total_items_added = 0 # Renamed for clarity
    media_type_skipped = 0
    cutoff_date_skipped = 0

    wanted_content = []
    try: # Add try block for source fetching
        if source_type == 'Overseerr':
            wanted_content = get_wanted_from_overseerr(versions_from_config)
        elif source_type == 'My Plex Watchlist':
            wanted_content = get_wanted_from_plex_watchlist(versions_from_config)
        elif source_type == 'My Plex RSS Watchlist':
            plex_rss_url = source_data.get('url', '')
            if not plex_rss_url:
                logging.error(f"Missing URL for source: {source_id}")
                return {'added': 0, 'processed': 0, 'cache_skipped': 0, 'media_type_skipped': 0, 'error': f"Missing URL for {source_id}"}
            wanted_content = get_wanted_from_plex_rss(plex_rss_url, versions_from_config)
        elif source_type == 'My Friends Plex RSS Watchlist':
            plex_rss_url = source_data.get('url', '')
            if not plex_rss_url:
                logging.error(f"Missing URL for source: {source_id}")
                return {'added': 0, 'processed': 0, 'cache_skipped': 0, 'media_type_skipped': 0, 'error': f"Missing URL for {source_id}"}
            wanted_content = get_wanted_from_friends_plex_rss(plex_rss_url, versions_from_config)
        elif source_type == 'Other Plex Watchlist':
            wanted_content = get_wanted_from_other_plex_watchlist(
                username=source_data.get('username', ''),
                token=source_data.get('token', ''),
                versions=versions_from_config
            )
        elif source_type == 'MDBList':
            mdblist_urls = source_data.get('urls', '').split(',')
            for mdblist_url in mdblist_urls:
                mdblist_url = mdblist_url.strip()
                if mdblist_url: # Check if url is not empty
                    wanted_content.extend(get_wanted_from_mdblists(mdblist_url, versions_from_config))
        elif source_type == 'Special Trakt Lists':
            update_trakt_settings(content_sources)
            wanted_content = get_wanted_from_special_trakt_lists(source_data, versions_from_config)
        elif source_type == 'Trakt Watchlist':
            update_trakt_settings(content_sources)
            wanted_content = get_wanted_from_trakt_watchlist(versions_from_config)
        elif source_type == 'Trakt Lists':
            update_trakt_settings(content_sources)
            trakt_lists = source_data.get('trakt_lists', '').split(',')
            for trakt_list in trakt_lists:
                trakt_list = trakt_list.strip()
                if trakt_list: # Check if list name is not empty
                    wanted_content.extend(get_wanted_from_trakt_lists(trakt_list, versions_from_config))
        elif source_type == 'Friends Trakt Watchlist':
            update_trakt_settings(content_sources)
            wanted_content = get_wanted_from_friend_trakt_watchlist(source_data, versions_from_config)
        elif source_type == 'Trakt Collection':
            update_trakt_settings(content_sources)
            wanted_content = get_wanted_from_trakt_collection(versions_from_config)
        elif source_type == 'Collected':
            wanted_content = get_wanted_from_collected()
        else:
            logging.warning(f"Unknown source type: {source_type}")
            # Optionally return an error or empty result here
            return {'added': 0, 'processed': 0, 'cache_skipped': 0, 'media_type_skipped': 0, 'error': f"Unknown source type {source_type}"}

    except Exception as fetch_error:
        logging.error(f"Error fetching content from {source_id}: {fetch_error}", exc_info=True)
        return {'added': 0, 'processed': 0, 'cache_skipped': 0, 'media_type_skipped': 0, 'error': f"Error fetching from {source_id}: {str(fetch_error)}"}

    logging.debug(f"Fetched {len(wanted_content)} raw items for {source_id}")

    if wanted_content:
        try: # Add try block for processing
            if isinstance(wanted_content, list) and len(wanted_content) > 0 and isinstance(wanted_content[0], tuple):
                # Handle list of tuples
                for items, item_versions_from_source_tuple in wanted_content: # Renamed item_versions to avoid conflict
                    batch_items_processed = 0
                    batch_total_items_added = 0
                    batch_cache_skipped = 0
                    batch_media_type_skipped = 0
                    batch_cutoff_date_skipped = 0

                    try:
                        logging.debug(f"Processing batch of {len(items)} items from {source_id}")

                        original_count = len(items)
                        # Filter by media type
                        if source_media_type != 'All' and not source_type.startswith('Collected'):
                            items_filtered_type = [
                                item for item in items
                                if (source_media_type == 'Movies' and item.get('media_type') == 'movie') or
                                   (source_media_type == 'Shows' and item.get('media_type') == 'tv')
                            ]
                            batch_media_type_skipped += original_count - len(items_filtered_type)
                            items = items_filtered_type # Update items after filtering
                            if batch_media_type_skipped > 0:
                                logging.debug(f"Batch {source_id}: Skipped {batch_media_type_skipped} items due to media type mismatch")

                        # Filter by cache
                        items_to_process_raw = [
                            item for item in items
                            if should_process_item(item, source_id, source_cache)
                        ]
                        batch_cache_skipped += len(items) - len(items_to_process_raw)
                        logging.debug(f"Batch {source_id}: Cache filtering results: {batch_cache_skipped} skipped, {len(items_to_process_raw)} to process")

                        if items_to_process_raw:
                            batch_items_processed += len(items_to_process_raw)
                            
                            # Convert versions from tuple if necessary
                            if isinstance(item_versions_from_source_tuple, list):
                                versions_to_inject = {v: True for v in item_versions_from_source_tuple}
                            elif isinstance(item_versions_from_source_tuple, dict):
                                versions_to_inject = item_versions_from_source_tuple
                            else:
                                logging.warning(f"Unexpected format for versions in tuple for {source_id}. Using main source versions dict.")
                                versions_to_inject = versions_from_config # Fallback to the converted source versions

                            # Inject the CONVERTED versions dictionary into each item
                            items_for_metadata = []
                            for item_dict_raw in items_to_process_raw:
                                item_dict_processed = item_dict_raw.copy()
                                item_dict_processed['versions'] = versions_to_inject # Inject the dict
                                items_for_metadata.append(item_dict_processed)

                            processed_items_meta = process_metadata(items_for_metadata)
                            if processed_items_meta:
                                all_items_meta = processed_items_meta.get('movies', []) + processed_items_meta.get('episodes', [])
                                for item in all_items_meta:
                                    item['content_source'] = source_id
                                    item = append_content_source_detail(item, source_type=source_type)

                                for item_original in items_to_process_raw: # Use raw items for cache update
                                    update_cache_for_item(item_original, source_id, source_cache)

                                from database import add_wanted_items
                                added_count = add_wanted_items(all_items_meta, versions_to_inject or versions_from_config) 
                                batch_total_items_added += added_count or 0

                                # Filter by cutoff date after metadata processing
                                if cutoff_date:
                                    items_filtered_date = []
                                    for item in all_items_meta:
                                        release_date = item.get('release_date')
                                        if not release_date or release_date.lower() == 'unknown':
                                            items_filtered_date.append(item)
                                            continue
                                        try:
                                            item_date = datetime.strptime(release_date, '%Y-%m-%d').date()
                                            if item_date >= cutoff_date:
                                                items_filtered_date.append(item)
                                            else:
                                                batch_cutoff_date_skipped += 1
                                                logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to cutoff date: {release_date} < {cutoff_date}")
                                        except ValueError:
                                            # If we can't parse the date, allow the item through
                                            items_filtered_date.append(item)
                                            logging.debug(f"Item {item.get('title', 'Unknown')} has invalid date format: {release_date}, allowing through")
                                    all_items_meta = items_filtered_date
                                    if batch_cutoff_date_skipped > 0:
                                        logging.debug(f"Batch {source_id}: Skipped {batch_cutoff_date_skipped} items due to cutoff date")

                    except Exception as batch_error:
                        logging.error(f"Error processing batch from {source_id}: {str(batch_error)}", exc_info=True)
                        # Continue to next batch

                    # Aggregate results from batch
                    items_processed += batch_items_processed
                    total_items_added += batch_total_items_added
                    cache_skipped += batch_cache_skipped
                    media_type_skipped += batch_media_type_skipped
                    cutoff_date_skipped += batch_cutoff_date_skipped

            else: # Handle single list of items (assuming this path is less common based on previous logic)
                original_count = len(wanted_content)
                # Filter by media type
                if source_media_type != 'All' and not source_type.startswith('Collected'):
                    wanted_content_filtered_type = [
                        item for item in wanted_content
                        if (source_media_type == 'Movies' and item.get('media_type') == 'movie') or
                           (source_media_type == 'Shows' and item.get('media_type') == 'tv')
                    ]
                    media_type_skipped += original_count - len(wanted_content_filtered_type)
                    wanted_content = wanted_content_filtered_type # Update wanted_content
                    if media_type_skipped > 0:
                        logging.debug(f"{source_id}: Skipped {media_type_skipped} items due to media type mismatch")

                # Filter by cache
                items_to_process_raw = [
                    item for item in wanted_content
                    if should_process_item(item, source_id, source_cache)
                ]
                cache_skipped += len(wanted_content) - len(items_to_process_raw)
                logging.debug(f"{source_id}: Cache filtering results: {cache_skipped} skipped, {len(items_to_process_raw)} to process")

                if items_to_process_raw:
                    items_processed += len(items_to_process_raw)

                    # Convert the CONVERTED versions dictionary into each item
                    items_for_metadata = []
                    for item_dict_raw in items_to_process_raw:
                        item_dict_processed = item_dict_raw.copy()
                        # Use the CONVERTED source-level versions_dict here
                        item_dict_processed['versions'] = versions_from_config 
                        items_for_metadata.append(item_dict_processed)
                        
                    processed_items_meta = process_metadata(items_for_metadata)
                    if processed_items_meta:
                        all_items_meta = processed_items_meta.get('movies', []) + processed_items_meta.get('episodes', [])
                        for item in all_items_meta:
                            item['content_source'] = source_id
                            item = append_content_source_detail(item, source_type=source_type)

                        for item_original in items_to_process_raw: # Use raw items for cache update
                            update_cache_for_item(item_original, source_id, source_cache)

                        from database import add_wanted_items
                        added_count = add_wanted_items(all_items_meta, versions_from_config) 
                        total_items_added += added_count or 0

                        # Filter by cutoff date after metadata processing
                        if cutoff_date:
                            items_filtered_date = []
                            for item in all_items_meta:
                                release_date = item.get('release_date')
                                if not release_date or release_date.lower() == 'unknown':
                                    items_filtered_date.append(item)
                                    continue
                                try:
                                    item_date = datetime.strptime(release_date, '%Y-%m-%d').date()
                                    if item_date >= cutoff_date:
                                        items_filtered_date.append(item)
                                    else:
                                        cutoff_date_skipped += 1
                                        logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to cutoff date: {release_date} < {cutoff_date}")
                                except ValueError:
                                    # If we can't parse the date, allow the item through
                                    items_filtered_date.append(item)
                                    logging.debug(f"Item {item.get('title', 'Unknown')} has invalid date format: {release_date}, allowing through")
                            all_items_meta = items_filtered_date
                            if cutoff_date_skipped > 0:
                                logging.debug(f"{source_id}: Skipped {cutoff_date_skipped} items due to cutoff date")

            # Save the updated cache
            save_source_cache(source_id, source_cache)
            logging.debug(f"Final cache state for {source_id}: {len(source_cache)} entries")

            stats_msg = f"Source {source_id}: Added {total_items_added} items"
            if items_processed > 0: stats_msg += f" (Processed {items_processed} items)"
            if cache_skipped > 0: stats_msg += f", Skipped {cache_skipped} (cache)"
            if media_type_skipped > 0: stats_msg += f", Skipped {media_type_skipped} (media type)"
            if cutoff_date_skipped > 0: stats_msg += f", Skipped {cutoff_date_skipped} (cutoff date)"
            logging.info(stats_msg)

        except Exception as process_error:
            logging.error(f"Error processing items from {source_id}: {str(process_error)}", exc_info=True)
            # Return counts accumulated so far, plus the error
            return {'added': total_items_added, 'processed': items_processed, 'cache_skipped': cache_skipped, 'media_type_skipped': media_type_skipped, 'cutoff_date_skipped': cutoff_date_skipped, 'error': f"Error processing items: {str(process_error)}"}

    else:
        logging.info(f"No wanted content retrieved from {source_id}")

    # Return the final counts
    return {'added': total_items_added, 'processed': items_processed, 'cache_skipped': cache_skipped, 'media_type_skipped': media_type_skipped, 'cutoff_date_skipped': cutoff_date_skipped}

def get_content_sources():
    """Get content sources from ProgramRunner instance."""
    program_runner = ProgramRunner()
    return program_runner.get_content_sources()

@debug_bp.route('/api/get_wanted_content', methods=['POST'])
@admin_required
def get_wanted_content():
    source_id = request.json.get('source_id', 'all')
    from routes.extensions import task_queue # Import the task_queue
    task_id = task_queue.add_task(async_get_wanted_content, source_id) # Use task_queue
    return jsonify({'task_id': task_id}), 202 # Return the real task_id and 202 Accepted

@debug_bp.route('/api/rate_limit_info')
def get_rate_limit_info():
    rate_limit_info = {}
    current_time = time.time()
    
    for domain in api.monitored_domains:
        hourly_calls = [t for t in api.rate_limiter.hourly_calls[domain] if t > current_time - 3600]
        five_minute_calls = [t for t in api.rate_limiter.five_minute_calls[domain] if t > current_time - 300]
        
        rate_limit_info[domain] = {
            'five_minute': {
                'count': len(five_minute_calls),
                'limit': api.rate_limiter.five_minute_limit
            },
            'hourly': {
                'count': len(hourly_calls),
                'limit': api.rate_limiter.hourly_limit
            }
        }
    
    return jsonify(rate_limit_info)

@debug_bp.route('/rescrape_item', methods=['POST'])
@admin_required
def rescrape_item():
    data = request.get_json()
    item_id = data.get('item_id')
    if not item_id:
        return jsonify({'success': False, 'error': 'Item ID is required'}), 400

    try:
        from database.database_reading import get_media_item_by_id
        # remove_file_from_plex is still needed if there are other direct calls,
        # but for this specific logic, we'll use the cache.

        # Get the item details first
        item = get_media_item_by_id(item_id)
        if not item:
            return jsonify({'success': False, 'error': 'Item not found'}), 404

        # Get file management settings
        file_management = get_setting('File Management', 'file_collection_management', 'Plex')
        mounted_location = get_setting('Plex', 'mounted_file_location', get_setting('File Management', 'original_files_path', ''))
        # original_files_path = get_setting('File Management', 'original_files_path', '') # Not directly used in this logic block
        # symlinked_files_path = get_setting('File Management', 'symlinked_files_path', '') # Not directly used

        # Handle file deletion based on management type
        if file_management == 'Plex' and (item['state'] == 'Collected' or item['state'] == 'Upgrading'):
            if mounted_location and item.get('location_on_disk'):
                try:
                    if os.path.exists(item['location_on_disk']):
                        os.remove(item['location_on_disk'])
                        logging.info(f"Rescrape: Deleted file {item['location_on_disk']} for item {item_id} (Plex mode).")
                except Exception as e:
                    logging.error(f"Error deleting file at {item['location_on_disk']}: {str(e)}")

            time.sleep(1) # Allow time for filesystem operations

            if item.get('filled_by_file'): # Ensure filled_by_file exists
                if item['type'] == 'movie':
                    cache_plex_removal(item['title'], item['filled_by_file'])
                    logging.info(f"Rescrape: Queued Plex removal via cache for movie {item['title']} (item {item_id}), path: {item['filled_by_file']}.")
                elif item['type'] == 'episode':
                    cache_plex_removal(item['title'], item['filled_by_file'], item.get('episode_title'))
                    logging.info(f"Rescrape: Queued Plex removal via cache for episode {item.get('episode_title')} of {item['title']} (item {item_id}), path: {item['filled_by_file']}.")
            else:
                logging.warning(f"Rescrape: Missing 'filled_by_file' for item {item_id} (Plex mode), cannot queue Plex removal.")

        elif file_management == 'Symlinked/Local' and (item['state'] == 'Collected' or item['state'] == 'Upgrading'):
            symlink_path_for_plex = None
            # Handle symlink removal
            if item.get('location_on_disk'):
                symlink_path_for_plex = item['location_on_disk'] # Store for potential Plex removal path
                try:
                    if os.path.exists(item['location_on_disk']) and os.path.islink(item['location_on_disk']):
                        os.unlink(item['location_on_disk'])
                        logging.info(f"Rescrape: Removed symlink {item['location_on_disk']} for item {item_id} (Symlinked/Local mode).")
                except Exception as e:
                    logging.error(f"Error removing symlink at {item['location_on_disk']}: {str(e)}")

            # Handle original file removal
            if item.get('original_path_for_symlink'):
                try:
                    if os.path.exists(item['original_path_for_symlink']):
                        os.remove(item['original_path_for_symlink'])
                        logging.info(f"Rescrape: Deleted original file {item['original_path_for_symlink']} for item {item_id} (Symlinked/Local mode).")
                except Exception as e:
                    logging.error(f"Error deleting original file at {item['original_path_for_symlink']}: {str(e)}")

            time.sleep(1) # Allow time for filesystem operations

            # Queue for Plex removal if configured
            plex_url = get_setting('File Management', 'plex_url_for_symlink', '')
            if plex_url:
                # For Symlinked/Local, Plex usually sees the symlink.
                # The path given to cache_plex_removal should be what Plex uses to identify the file.
                # remove_file_from_plex matches basenames.
                path_to_tell_plex = None
                if symlink_path_for_plex: # Prefer the symlink path's basename if it existed
                    path_to_tell_plex = os.path.basename(symlink_path_for_plex)
                elif item.get('original_path_for_symlink'): # Fallback to original file's basename
                     path_to_tell_plex = os.path.basename(item['original_path_for_symlink'])

                if path_to_tell_plex:
                    if item['type'] == 'movie':
                        cache_plex_removal(item['title'], path_to_tell_plex)
                        logging.info(f"Rescrape: Queued Plex removal via cache for movie {item['title']} (item {item_id}), path: {path_to_tell_plex} (Symlinked/Local mode).")
                    elif item['type'] == 'episode':
                        cache_plex_removal(item['title'], path_to_tell_plex, item.get('episode_title'))
                        logging.info(f"Rescrape: Queued Plex removal via cache for episode {item.get('episode_title')} of {item['title']} (item {item_id}), path: {path_to_tell_plex} (Symlinked/Local mode).")
                else:
                    logging.warning(f"Rescrape: No valid path (symlink or original) found for Plex removal for item {item_id} (Symlinked/Local mode).")
            else:
                logging.info(f"Rescrape: Plex URL for symlink not configured, skipping Plex removal for item {item_id} (Symlinked/Local mode).")


        # Move the item to Wanted queue
        move_item_to_wanted(item_id, item.get('original_scraped_torrent_title')) # Pass the original_scraped_torrent_title
        logging.info(f"Rescrape: Moved item {item_id} to Wanted queue.")
        return jsonify({'success': True, 'message': 'Item files processed, Plex removal cached (if applicable), and item moved to Wanted queue for rescraping'}), 200
    except Exception as e:
        logging.error(f"Error rescraping item {data.get('item_id', 'N/A')}: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

def move_item_to_wanted(item_id, current_original_scraped_title=None):
    from database import get_db_connection
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE media_items 
            SET state = 'Wanted', 
                filled_by_file = NULL, 
                filled_by_title = NULL, 
                filled_by_magnet = NULL, 
                filled_by_torrent_id = NULL, 
                collected_at = NULL,
                last_updated = ?,
                location_on_disk = NULL,
                original_path_for_symlink = NULL,
                rescrape_original_torrent_title = ?,
                original_scraped_torrent_title = NULL,
                upgrading_from = NULL,
                version = TRIM(version, '*'),
                upgrading = NULL,
                fall_back_to_single_scraper = 0,
                upgraded = NULL
            WHERE id = ?
        ''', (datetime.now(), current_original_scraped_title, item_id))
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


@debug_bp.route('/send_test_notification', methods=['POST'])
@admin_required
def send_test_notification():
    current_app.logger.info("Entering send_test_notification function")
    try:
        # Create test notification items
        now = datetime.now()
        
        # Collection test notifications
        collection_notifications = [
            {
                'type': 'movie',
                'title': 'Test Movie 1',
                'year': 2023,
                'tmdb_id': '123456',
                'original_collected_at': now.isoformat(),
                'version': '1080p',
                'is_upgrade': False,
                'media_type': 'movie',
                'new_state': 'Collected',  # Adding new_state for NEW indicator
                'content_source': 'My Plex Watchlist',
                'content_source_detail': 'user1'
            },
            {
                'type': 'movie',
                'title': 'Test Movie 2',
                'year': 2023,
                'tmdb_id': '234567',
                'original_collected_at': (now + timedelta(hours=1)).isoformat(),
                'version': '2160p',
                'is_upgrade': True,
                'upgrading_from': 'Test Movie 2 1080p.mkv',
                'media_type': 'movie',
                'new_state': 'Collected',  # Adding new_state for upgrade indicator
                'content_source': 'Trakt Watchlist',
                'content_source_detail': 'user2'
            },
            {
                'type': 'episode',
                'title': 'Test TV Show 1',
                'year': 2023,
                'tmdb_id': '345678',
                'season_number': 1,
                'episode_number': 1,
                'original_collected_at': (now + timedelta(hours=2)).isoformat(),
                'version': 'Default',
                'is_upgrade': False,
                'media_type': 'tv',
                'new_state': 'Collected',
                'content_source': 'Overseerr',
                'content_source_detail': 'user3'
            },
            {
                'type': 'episode',
                'title': 'Test TV Show 3',
                'year': 2023,
                'tmdb_id': '789012',
                'season_number': 1,
                'episode_number': 3,
                'original_collected_at': (now + timedelta(hours=3)).isoformat(),
                'version': '2160p',
                'is_upgrade': True,
                'upgrading_from': 'Test TV Show 3 S01E03 1080p.mkv',
                'media_type': 'tv',
                'new_state': 'Collected',  # This indicates it's a completed upgrade
                'content_source': 'Trakt Lists',
                'content_source_detail': 'user4'
            }
        ]

        # State change test notifications
        state_change_notifications = [
            {
                'type': 'movie',
                'title': 'Test Movie 3',
                'year': 2023,
                'tmdb_id': '456789',
                'version': '1080p',
                'new_state': 'Checking',
                'is_upgrade': False,
                'upgrading_from': None,
                'media_type': 'movie',
                'content_source': 'MDBList',
                'content_source_detail': 'user5'
            },
            {
                'type': 'movie',
                'title': 'Test Movie 4',
                'year': 2023,
                'tmdb_id': '567890',
                'version': '2160p',
                'new_state': 'Sleeping',
                'is_upgrade': False,
                'upgrading_from': None,
                'media_type': 'movie',
                'content_source': 'My Plex RSS Watchlist',
                'content_source_detail': 'user6'
            },
            {
                'type': 'episode',
                'title': 'Test TV Show 2',
                'year': 2023,
                'tmdb_id': '678901',
                'season_number': 1,
                'episode_number': 2,
                'version': '1080p',
                'new_state': 'Upgrading',
                'is_upgrade': True,
                'upgrading_from': 'Test TV Show 2 S01E02 720p.mkv',
                'media_type': 'tv',
                'content_source': 'Other Plex Watchlist',
                'content_source_detail': 'user7'
            }
        ]

        # Fetch enabled notifications
        enabled_notifications = get_all_settings().get('Notifications', {})
        
        # Send collection notifications
        send_notifications(collection_notifications, enabled_notifications, notification_category='collected')
        
        # Send state change notifications
        send_notifications(state_change_notifications, enabled_notifications, notification_category='state_change')
        
        return jsonify({'success': True, 'message': 'Test notifications sent successfully'}), 200

    except Exception as e:
        current_app.logger.error(f"Error sending test notification: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': 'An error occurred while sending the test notification. Please check the server logs for more details.'}), 500
    
@debug_bp.route('/move_to_upgrading', methods=['POST'])
@admin_required
def move_to_upgrading():
    item_id = request.form.get('item_id')
    if not item_id:
        return jsonify({'success': False, 'error': 'Item ID is required'}), 400

    try:
        from database import get_db_connection
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE media_items 
            SET state = 'Upgrading',
                last_updated = ?
            WHERE id = ? AND state = 'Collected'
        ''', (datetime.now(), item_id))
        conn.commit()
        
        if cursor.rowcount > 0:
            return jsonify({'success': True, 'message': f'Item {item_id} moved to Upgrading state'}), 200
        else:
            return jsonify({'success': False, 'error': f'Item {item_id} not found or not in Collected state'}), 404
    except Exception as e:
        logging.error(f"Error moving item to Upgrading state: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()

@debug_bp.route('/run_task', methods=['POST'])
@admin_required
def run_task():
    """Manually trigger a task by adding it to the APScheduler queue."""
    try:
        data = request.get_json()
        task_name = data.get('task_name')
        if not task_name:
            return jsonify({'success': False, 'error': 'Task name not provided'}), 400

        runner = get_program_runner() # This should now work
        if not runner:
            return jsonify({'success': False, 'error': 'ProgramRunner not initialized'}), 500

        # trigger_task now returns a dict or raises an exception
        result = runner.trigger_task(task_name) 
        
        # result will be like {"success": True, "message": "Task 'X' queued...", "job_id": "manual_X_uuid"}
        return jsonify(result), 200

    except ValueError as ve: # Catch specific errors from trigger_task (e.g., task not defined)
        logging.error(f"ValueError in run_task: {ve}")
        return jsonify({'success': False, 'error': str(ve)}), 400
    except RuntimeError as re: # Catch specific errors from trigger_task (e.g., queueing failed)
        logging.error(f"RuntimeError in run_task: {re}")
        return jsonify({'success': False, 'error': str(re)}), 500
    except Exception as e:
        logging.error(f"Error in run_task: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': f'An unexpected error occurred: {str(e)}'}), 500

@debug_bp.route('/get_available_tasks', methods=['GET'])
@admin_required
def get_available_tasks():
    # Define the task list with display names
    task_map = [
        {'id': 'wanted', 'display_name': 'Wanted'},
        {'id': 'scraping', 'display_name': 'Scraping'},
        {'id': 'adding', 'display_name': 'Adding'},
        {'id': 'checking', 'display_name': 'Checking'},
        {'id': 'sleeping', 'display_name': 'Sleeping'},
        {'id': 'unreleased', 'display_name': 'Unreleased'},
        {'id': 'blacklisted', 'display_name': 'Blacklisted'},
        {'id': 'pending_uncached', 'display_name': 'Pending Uncached'},
        {'id': 'upgrading', 'display_name': 'Upgrading'},
        {'id': 'task_plex_full_scan', 'display_name': 'Plex Full Scan'},
        {'id': 'task_debug_log', 'display_name': 'Debug Log'},
        {'id': 'task_refresh_release_dates', 'display_name': 'Refresh Release Dates'},
        {'id': 'task_purge_not_wanted_magnets_file', 'display_name': 'Purge Not Wanted Magnets File'},
        {'id': 'task_generate_airtime_report', 'display_name': 'Generate Airtime Report'},
        {'id': 'task_check_service_connectivity', 'display_name': 'Check Service Connectivity'},
        {'id': 'task_send_notifications', 'display_name': 'Send Notifications'},
        {'id': 'task_check_trakt_early_releases', 'display_name': 'Check Trakt Early Releases'},
        {'id': 'task_reconcile_queues', 'display_name': 'Reconcile Queues'},
        {'id': 'task_check_plex_files', 'display_name': 'Check Plex Files'},
        {'id': 'task_update_show_ids', 'display_name': 'Update Show IDs'},
        {'id': 'task_update_show_titles', 'display_name': 'Update Show Titles'},
        {'id': 'task_get_plex_watch_history', 'display_name': 'Get Plex Watch History'},
        {'id': 'task_check_database_health', 'display_name': 'Check Database Health'},
        {'id': 'task_run_library_maintenance', 'display_name': 'Run Library Maintenance'},
        {'id': 'task_update_movie_ids', 'display_name': 'Update Movie IDs'},
        {'id': 'task_update_movie_titles', 'display_name': 'Update Movie Titles'},
        {'id': 'task_verify_symlinked_files', 'display_name': 'Verify Symlinked Files'},
        {'id': 'task_verify_plex_removals', 'display_name': 'Verify Plex Removals'},
        {'id': 'task_process_pending_rclone_paths', 'display_name': 'Process Pending Rclone Paths'},
        {'id': 'task_update_tv_show_status', 'display_name': 'Update TV Show Status'},
        {'id': 'task_heartbeat', 'display_name': 'Heartbeat'},
        {'id': 'final_check_queue', 'display_name': 'Final Check Queue'},
        {'id': 'task_analyze_library', 'display_name': 'Analyze Library'}
    ]
    
    # Get content sources from program runner for content source tasks
    program_runner = ProgramRunner()
    content_sources = program_runner.get_content_sources()
    
    # Add content source tasks with display names from config
    for source_name, source_config in content_sources.items():
        if isinstance(source_config, dict) and source_config.get('enabled', False):
            task_id = f"task_{source_name}_wanted"
            
            # Use custom display name if available, otherwise format the source name
            if source_config.get('display_name'):
                display_name = f"Process Content Source: {source_config['display_name']}"
            else:
                formatted_name = ' '.join(word.capitalize() for word in source_name.split('_'))
                display_name = f"Process Content Source: {formatted_name}"
                
            task_map.append({'id': task_id, 'display_name': display_name})
    
    # For backward compatibility, also include the flat list of task IDs
    task_ids = [task['id'] for task in task_map]
    
    return jsonify({
        'tasks': task_ids,  # For backward compatibility
        'task_map': task_map  # New structured format with display names
    }), 200

@debug_bp.route('/not_wanted')
@admin_required
def not_wanted():
    config = load_config()
    not_wanted_magnets = get_not_wanted_magnets()
    urls = get_not_wanted_urls()
    return render_template('debug_not_wanted.html', magnets=not_wanted_magnets, urls=urls)

@debug_bp.route('/not_wanted/magnet/remove', methods=['POST'])
@admin_required
def remove_not_wanted_magnet():
    magnet_hash = request.form.get('hash')
    if not magnet_hash:
        return jsonify({'success': False, 'error': 'Magnet hash is required'}), 400

    try:
        magnets = get_not_wanted_magnets()
        if magnet_hash in magnets:
            magnets.remove(magnet_hash)
            save_not_wanted_magnets(magnets)
            return jsonify({'success': True, 'message': 'Magnet removed from not wanted list.'}), 200
        else:
            return jsonify({'success': False, 'error': 'Magnet not found in not wanted list.'}), 404
    except Exception as e:
        logging.error(f"Error removing not wanted magnet: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@debug_bp.route('/not_wanted/url/remove', methods=['POST'])
@admin_required
def remove_not_wanted_url():
    url_to_remove = request.form.get('url')
    if not url_to_remove:
        return jsonify({'success': False, 'error': 'URL is required'}), 400

    try:
        urls = get_not_wanted_urls()
        if url_to_remove in urls:
            urls.remove(url_to_remove)
            save_not_wanted_urls(urls)
            return jsonify({'success': True, 'message': 'URL removed from not wanted list.'}), 200
        else:
            return jsonify({'success': False, 'error': 'URL not found in not wanted list.'}), 404
    except Exception as e:
        logging.error(f"Error removing not wanted URL: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@debug_bp.route('/not_wanted/purge', methods=['POST'])
@admin_required
def purge_not_wanted():
    purge_type = request.form.get('purge_type')
    
    if purge_type == 'magnets':
        try:
            save_not_wanted_magnets(set())
            return jsonify({'success': True, 'message': 'All not wanted magnets have been purged.'}), 200
        except Exception as e:
            logging.error(f"Error purging not wanted magnets: {str(e)}")
            return jsonify({'success': False, 'error': str(e)}), 500
    elif purge_type == 'urls':
        try:
            save_not_wanted_urls(set())
            return jsonify({'success': True, 'message': 'All not wanted URLs have been purged.'}), 200
        except Exception as e:
            logging.error(f"Error purging not wanted URLs: {str(e)}")
            return jsonify({'success': False, 'error': str(e)}), 500
    else:
        return jsonify({'success': False, 'error': 'Invalid purge type'}), 400

@debug_bp.route('/propagate_version', methods=['POST'])
@admin_required
def propagate_version():
    try:
        original_version = request.form.get('original_version', '').strip('*')
        propagated_version = request.form.get('propagated_version', '').strip('*')
        media_type = request.form.get('media_type', 'all')
        
        logging.info(f"Starting version propagation from {original_version} to {propagated_version} for media type: {media_type}")
        
        if not original_version or not propagated_version:
            return jsonify({'success': False, 'error': 'Both versions are required'})
        
        from database import get_db_connection
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Build the base query with media type filter
        base_query = """
            SELECT title, year, type, imdb_id, tmdb_id,
                   episode_title, season_number, episode_number,
                   airtime, release_date
            FROM media_items 
            WHERE REPLACE(version, '*', '') = ?
        """
        
        query_params = [original_version]
        
        if media_type != 'all':
            base_query += " AND type = ?"
            query_params.append(media_type)
            
        cursor.execute(base_query, query_params)
        items = cursor.fetchall()
        
        logging.info(f"Found {len(items)} items with version {original_version}")
        
        # For each item, check if propagated version exists (including asterisk variations)
        added_count = 0
        for item in items:
            cursor.execute("""
                SELECT COUNT(*) 
                FROM media_items 
                WHERE title = ? 
                AND year = ? 
                AND type = ? 
                AND COALESCE(season_number, -1) = COALESCE(?, -1)
                AND COALESCE(episode_number, -1) = COALESCE(?, -1)
                AND REPLACE(version, '*', '') = ?
            """, (
                item['title'], item['year'], item['type'],
                item['season_number'], item['episode_number'],
                propagated_version
            ))
            exists = cursor.fetchone()[0] > 0
            
            if not exists:
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                logging.debug(f"Adding {propagated_version} version for {item['title']} ({item['year']}) - " + 
                           (f"S{item['season_number']}E{item['episode_number']}" if item['type'] == 'episode' else 'movie'))
                
                # Add as wanted with propagated version
                cursor.execute("""
                    INSERT INTO media_items (
                        title, year, type, imdb_id, tmdb_id,
                        episode_title, season_number, episode_number,
                        airtime, release_date,
                        version, state, last_updated, metadata_updated
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Wanted', ?, ?)
                """, (
                    item['title'], item['year'], item['type'],
                    item['imdb_id'], item['tmdb_id'],
                    item['episode_title'], item['season_number'], item['episode_number'],
                    item['airtime'], item['release_date'],
                    propagated_version, now, now
                ))
                added_count += 1
        
        conn.commit()
        conn.close()
        
        logging.info(f"Successfully added {added_count} items with version {propagated_version}")
        message = f'Successfully added {added_count} items with version {propagated_version}'
        return jsonify({'success': True, 'message': message})
        
    except Exception as e:
        logging.error(f"Error in propagate_version: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})

def get_available_versions():
    config = load_config()
    versions = []
    
    # Get versions from Scraping.versions
    scraping_config = config.get('Scraping', {})
    version_configs = scraping_config.get('versions', {})
    
    # Add versions from config
    for version in version_configs.keys():
        clean_version = version.strip('*')
        if clean_version:
            versions.append(clean_version)
    
    # Get versions from the database as backup
    if not versions:
        from database import get_db_connection
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT version FROM media_items WHERE version IS NOT NULL")
        versions = [row['version'].strip('*') for row in cursor.fetchall()]
        conn.close()
    
    return sorted(versions)

@debug_bp.route('/get_versions', methods=['GET'])
@admin_required
def get_versions():
    try:
        versions = get_available_versions()
        return jsonify({'success': True, 'versions': versions})
    except Exception as e:
        logging.error(f"Error getting versions: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})

@debug_bp.route('/convert_to_symlinks', methods=['POST'])
@admin_required
def convert_to_symlinks():
    """Convert existing library items to use symlinks."""
    try:
        import uuid
        
        # Generate unique task ID
        task_id = str(uuid.uuid4())
        
        def run_conversion():
            try:
                import os
                # Get database connection
                from database import get_db_connection
                conn = get_db_connection()
                cursor = conn.cursor()

                # Get symlinked files path from settings
                symlinked_path = get_setting('File Management', 'symlinked_files_path', '/mnt/symlinked')
                if not symlinked_path:
                    scan_progress[task_id].update({
                        'status': 'error',
                        'message': 'Symlinked files path not configured',
                        'complete': True
                    })
                    return

                # Get all items with location_on_disk set
                cursor.execute("""
                    SELECT *
                    FROM media_items 
                    WHERE location_on_disk IS NOT NULL 
                    AND location_on_disk != ''
                    AND state = 'Collected'
                """)
                items = cursor.fetchall()

                if not items:
                    scan_progress[task_id].update({
                        'status': 'error',
                        'message': 'No items found with location_on_disk set',
                        'complete': True
                    })
                    return

                total_items = len(items)
                logging.info(f"Found {total_items} items to convert to symlinks")

                # Initialize progress tracking
                scan_progress[task_id].update({
                    'status': 'running',
                    'message': f'Converting {total_items} items to symlinks...',
                    'total_items': total_items,
                    'processed_items': 0,
                    'symlinks_created': 0,
                    'items_to_wanted': 0,
                    'items_deleted': 0,
                    'items_skipped': 0,
                    'complete': False
                })

                # Convert items to symlinks
                processed = 0
                wanted_count = 0
                deleted_count = 0
                skipped_count = 0
                symlinks_created = 0
                found_symlinks = False
                check_for_symlinks = True

                for item in items:
                    item_dict = dict(item)
                    
                    # Check if item is already in symlink folder
                    current_location = item_dict.get('location_on_disk', '')
                    
                    # If the current location is in the symlink folder, skip it
                    if current_location.startswith(symlinked_path):
                        logging.info(f"Skipping {item_dict['title']} ({item_dict['version']}) - already in symlink folder")
                        skipped_count += 1
                        scan_progress[task_id].update({
                            'items_skipped': skipped_count,
                            'message': f'Skipping {item_dict["title"]} - already in symlink folder'
                        })
                        continue

                    # Only check for symlinks in first 100 items unless we've found one
                    if check_for_symlinks:
                        try:
                            if os.path.islink(current_location):
                                real_path = os.path.realpath(current_location)
                                logging.info(f"Found symlink for {item_dict['title']}, using original path: {real_path}")
                                # Store both the real path and the original filename
                                item_dict['filename_real_path'] = os.path.basename(real_path)
                                # Keep the original location_on_disk as is - don't resolve it yet
                                found_symlinks = True
                                logging.debug(f"Set filename_real_path to: {item_dict['filename_real_path']}")
                        except Exception as e:
                            logging.warning(f"Error checking symlink for {current_location}: {str(e)}")

                        if processed >= 100 and not found_symlinks:
                            logging.info("No symlinks found in first 100 items, disabling symlink check")
                            check_for_symlinks = False

                    result = convert_item_to_symlink(item_dict, skip_verification=True) # Pass skip_verification=True here

                    if result['success']:
                        symlinks_created += 1
                        # Update database with new location and original path
                        cursor.execute("""
                            UPDATE media_items 
                            SET location_on_disk = ?,
                                original_path_for_symlink = ?
                            WHERE id = ?
                        """, (result['new_location'], result['old_location'], result['item_id']))
                    else:
                        # If error is "Source file not found", handle specially
                        if "Source file not found" in result['error']:
                            # Check for duplicate
                            cursor.execute("""
                                SELECT COUNT(*) as count 
                                FROM media_items 
                                WHERE title = ? 
                                AND type = ? 
                                AND TRIM(version, '*') = TRIM(?, '*')
                                AND state IN ('Wanted', 'Collected')
                                AND id != ?
                            """, (item_dict['title'], item_dict['type'], item_dict['version'], item_dict['id']))
                            
                            has_duplicate = cursor.fetchone()['count'] > 0
                            
                            if has_duplicate:
                                # Delete this item as we already have a copy
                                cursor.execute("DELETE FROM media_items WHERE id = ?", (result['item_id'],))
                                deleted_count += 1
                                logging.info(f"Deleted item {item_dict['title']} as duplicate exists")
                            else:
                                # Update the item to Wanted state
                                cursor.execute("""
                                    UPDATE media_items 
                                    SET state = 'Wanted',
                                        filled_by_file = NULL,
                                        filled_by_title = NULL,
                                        filled_by_magnet = NULL,
                                        filled_by_torrent_id = NULL,
                                        collected_at = NULL,
                                        location_on_disk = NULL,
                                        last_updated = CURRENT_TIMESTAMP,
                                        version = TRIM(version, '*')
                                    WHERE id = ?
                                """, (result['item_id'],))
                                wanted_count += 1
                                logging.info(f"Moved item {item_dict['title']} to Wanted state")
                    
                    processed += 1
                    
                    # Update progress
                    scan_progress[task_id].update({
                        'processed_items': processed,
                        'symlinks_created': symlinks_created,
                        'items_to_wanted': wanted_count,
                        'items_deleted': deleted_count,
                        'items_skipped': skipped_count,
                        'message': f'Processing: {item_dict["title"]}'
                    })
                    
                    # Commit every 50 items
                    if processed % 50 == 0:
                        conn.commit()

                conn.commit()
                conn.close()

                # Final status update
                scan_progress[task_id].update({
                    'status': 'complete',
                    'message': 'Library conversion completed successfully',
                    'complete': True,
                    'success': True
                })

            except Exception as e:
                logging.error(f"Error during library conversion: {str(e)}", exc_info=True)
                scan_progress[task_id].update({
                    'status': 'error',
                    'message': f'Error during conversion: {str(e)}',
                    'complete': True,
                    'success': False
                })
            finally:
                # Clean up progress tracking after 5 minutes
                threading.Timer(300, lambda: scan_progress.pop(task_id, None)).start()

        # Initialize progress tracking
        scan_progress[task_id] = {
            'status': 'starting',
            'message': 'Initializing library conversion...',
            'complete': False
        }

        # Start conversion in background thread
        thread = threading.Thread(target=run_conversion)
        thread.daemon = True
        thread.start()

        return jsonify({'success': True, 'task_id': task_id})

    except Exception as e:
        logging.error(f"Error initiating library conversion: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)})

@debug_bp.route('/api/conversion_progress/<task_id>')
def conversion_progress(task_id):
    """SSE endpoint for tracking library conversion progress."""
    def generate():
        while True:
            if task_id not in scan_progress:
                yield f"data: {json.dumps({'status': 'error', 'message': 'Conversion task not found'})}\n\n"
                break
                
            progress = scan_progress[task_id]
            yield f"data: {json.dumps(progress)}\n\n"
            
            if progress['complete']:
                break
                
            time.sleep(1)
    
    return Response(stream_with_context(generate()), mimetype='text/event-stream')

@debug_bp.route('/validate_plex_tokens', methods=['GET', 'POST'])
@admin_required
def validate_plex_tokens_route():
    """Route to validate and refresh Plex tokens"""
    from content_checkers.plex_watchlist import validate_plex_tokens
    from content_checkers.plex_token_manager import get_token_status
    
    try:
        if request.method == 'POST':
            # For POST requests, perform a fresh validation
            token_status = validate_plex_tokens()
        else:
            # For GET requests, return the stored status
            token_status = get_token_status()
            if not token_status:
                # If no stored status exists, perform a fresh validation
                token_status = validate_plex_tokens()
        
        # Ensure all datetime objects are serialized
        for username, status in token_status.items():
            if isinstance(status.get('expires_at'), datetime):
                status['expires_at'] = status['expires_at'].isoformat()
            if isinstance(status.get('last_checked'), datetime):
                status['last_checked'] = status['last_checked'].isoformat()
        
        return jsonify({
            'success': True,
            'token_status': token_status
        })
    except Exception as e:
        logging.error(f"Error in validate_plex_tokens route: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        })
            
@debug_bp.route('/simulate_crash')
@admin_required
def simulate_crash():
    """Route to simulate a program crash for testing notifications."""
    from utilities.settings import get_setting
    if not get_setting('Debug', 'enable_crash_test', False):
        return jsonify({'success': False, 'error': 'Crash simulation is not enabled'}), 400
        
    # First send the crash notification
    from routes.notifications import send_program_crash_notification
    send_program_crash_notification("Simulated crash for testing notifications")
    
    # Then force an immediate crash with os._exit
    import os
    os._exit(1)  # This will force an immediate program termination

@debug_bp.route('/torrent_tracking')
@admin_required
def torrent_tracking():
    """View the torrent tracking history."""
    try:
        # Get the most recent 1000 entries
        entries = get_recent_additions(1000)
        
        # Convert the entries to a list of dictionaries for easier template handling
        formatted_entries = []
        if entries:  # Check if entries exist
            for entry in entries:
                formatted_entry = {
                    'id': entry[0],
                    'torrent_hash': entry[1],
                    'timestamp': entry[2],
                    'trigger_source': entry[3],
                    'trigger_details': entry[4],
                    'rationale': entry[5],
                    'item_data': entry[6],
                    'is_still_present': bool(entry[7]),
                    'removal_reason': entry[8],
                    'removal_timestamp': entry[9],
                    'additional_metadata': entry[10]
                }
                formatted_entries.append(formatted_entry)
        
        # Always render the template, even with empty entries
        return render_template('torrent_tracking.html', entries=formatted_entries)
    except Exception as e:
        logging.error(f"Error in torrent tracking view: {e}")
        flash(f"Error loading torrent tracking data: {str(e)}", 'error')
        return redirect(url_for('debug.debug_functions'))

@debug_bp.route('/verify_torrent/<hash_value>')
@admin_required
def verify_torrent(hash_value):
    """Verify if a torrent is still present in Real-Debrid and get its status"""
    try:
        debrid_provider = get_debrid_provider()
        
        # Get all active torrents from Real-Debrid
        logging.info("Fetching list of active torrents from Real-Debrid")
        try:
            active_torrents = debrid_provider.list_active_torrents()
            logging.info(f"Found {len(active_torrents)} active torrents")
        except Exception as e:
            if "429" in str(e):
                logging.warning("Rate limit hit while fetching active torrents")
                return jsonify({
                    'error': 'Rate limit exceeded. Please try again in a few seconds.',
                    'is_present': None,
                    'status': 'rate_limited'
                }), 429
            else:
                logging.error(f"Error fetching active torrents: {str(e)}")
                return jsonify({
                    'error': f"Failed to fetch active torrents: {str(e)}",
                    'is_present': None,
                    'status': 'error'
                }), 500
        
        # Find matching torrent
        logging.info(f"Searching for torrent with hash {hash_value}")
        matching_torrent = None
        for torrent in active_torrents:
            if torrent.get('hash', '').lower() == hash_value.lower():
                matching_torrent = torrent
                logging.info(f"Found matching torrent with ID: {torrent.get('id')}")
                break
        
        # Get any removal reason if it exists
        logging.info("Checking torrent history for removal information")
        history = get_torrent_history(hash_value)
        removal_reason = None
        
        if history and not history[0]['is_still_present']:
            removal_reason = history[0]['removal_reason']
            logging.info(f"Found removal reason in history: {removal_reason}")
        
        if matching_torrent:
            # Get detailed torrent info to check status
            logging.info(f"Getting detailed info for torrent ID: {matching_torrent['id']}")
            try:
                torrent_info = debrid_provider.get_torrent_info(matching_torrent['id'])
                if torrent_info:
                    status = torrent_info.get('status', '')
                    logging.info(f"Torrent status: {status}")
                    
                    if status == 'downloaded':
                        logging.info("Torrent is present and downloaded")
                        return jsonify({
                            'is_present': True,
                            'status': status,
                            'removal_reason': None
                        })
                    elif status in ['magnet_error', 'error', 'virus', 'dead']:
                        logging.warning(f"Torrent has error status: {status}")
                        return jsonify({
                            'is_present': False,
                            'status': status,
                            'removal_reason': f"Torrent error: {status}"
                        })
                    else:
                        logging.info(f"Torrent is present with status: {status}")
                        return jsonify({
                            'is_present': True,
                            'status': status,
                            'removal_reason': None
                        })
            except Exception as e:
                if "429" in str(e):
                    logging.warning("Rate limit hit while fetching torrent info")
                    return jsonify({
                        'error': 'Rate limit exceeded. Please try again in a few seconds.',
                        'is_present': None,
                        'status': 'rate_limited'
                    }), 429
                else:
                    logging.error(f"Error getting torrent info: {str(e)}")
                    return jsonify({
                        'error': f"Failed to get torrent info: {str(e)}",
                        'is_present': None,
                        'status': 'error'
                    }), 500
        
        # If we get here, the torrent was not found
        logging.info("Torrent not found in active torrents")
        return jsonify({
            'is_present': False,
            'status': 'not_found',
            'removal_reason': removal_reason
        })
        
    except Exception as e:
        logging.error(f"Error verifying torrent {hash_value}: {str(e)}", exc_info=True)
        return jsonify({
            'error': f"Verification failed: {str(e)}",
            'is_present': None,
            'status': 'error'
        }), 500

@debug_bp.route('/api/trakt_token_status', methods=['GET'])
@admin_required
def get_trakt_token_status():
    try:
        from cli_battery.app.trakt_auth import TraktAuth
        trakt_auth = TraktAuth()
        
        token_data = trakt_auth.get_token_data()
        last_refresh = trakt_auth.get_last_refresh_time()
        expires_at = trakt_auth.get_expiration_time()
        
        logging.debug(f"Trakt token status - Token Data: {token_data}")
        logging.debug(f"Trakt token status - Last Refresh: {last_refresh}")
        logging.debug(f"Trakt token status - Expires At: {expires_at}")
        
        # Ensure last_refresh is included in both places for compatibility
        token_data['last_refresh'] = last_refresh
        
        status = {
            'is_authenticated': trakt_auth.is_authenticated(),
            'token_data': token_data,
            'last_refresh': last_refresh,
            'expires_at': expires_at
        }
        
        logging.debug(f"Trakt token status response: {status}")
        
        return jsonify({
            'success': True,
            'status': status
        })
    except Exception as e:
        logging.error(f"Error getting Trakt token status: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        })

@debug_bp.route('/get_verification_queue', methods=['GET'])
@admin_required
def get_verification_queue():
    """Get the contents of the symlink verification queue."""
    try:
        # Get verification stats
        stats = get_verification_stats()
        
        # Get unverified files (limit to 500 to prevent overwhelming the UI)
        unverified_files = get_unverified_files(limit=500)
        
        # Format the data for display
        formatted_files = []
        for file in unverified_files:
            if file['type'] == 'episode':
                title = f"{file['title']} - S{file['season_number']:02d}E{file['episode_number']:02d}"
                if file['episode_title']:
                    title += f" - {file['episode_title']}"
            else:
                title = file['title']
                
            formatted_files.append({
                'id': file['verification_id'],
                'title': title,
                'filename': file['filename'],
                'full_path': file['full_path'],
                'media_item_id': file['media_item_id'],
                'added_at': file['added_at'],
                'attempts': file['verification_attempts'],
                'last_attempt': file['last_attempt'],
                'type': file['type']
            })
        
        return jsonify({
            'success': True,
            'stats': stats,
            'files': formatted_files
        })
    except Exception as e:
        logging.error(f"Error getting verification queue: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        })

@debug_bp.route('/test_get_torrent_files', methods=['POST'])
@admin_required
def test_get_torrent_files():
    """Test the get_torrent_file_list function from the RealDebridProvider."""
    magnet_link = request.form.get('magnet_link')
    if not magnet_link or not magnet_link.startswith('magnet:'):
        return jsonify({'success': False, 'error': 'Valid magnet link is required'}), 400

    try:
        provider = get_debrid_provider()
        if not provider:
            return jsonify({'success': False, 'error': 'Debrid provider not configured or unavailable'}), 500

        # Assuming RealDebridProvider for this specific test
        if not hasattr(provider, 'get_torrent_file_list'):
            return jsonify({'success': False, 'error': 'Provider does not support get_torrent_file_list'}), 501
        
        logging.info(f"Testing get_torrent_file_list with magnet: {magnet_link[:60]}...")
        file_list = provider.get_torrent_file_list(magnet_link)

        if file_list is not None:
            logging.info(f"Successfully retrieved {len(file_list)} files.")
            return jsonify({'success': True, 'file_list': file_list})
        else:
            logging.error("Failed to retrieve file list from provider.")
            return jsonify({'success': False, 'error': 'Failed to retrieve file list. Check logs for details.'}), 500

    except Exception as e:
        # Catch specific errors if needed, otherwise generic error
        error_msg = f"Error testing get_torrent_file_list: {str(e)}"
        logging.error(error_msg, exc_info=True)
        # Check for specific error types if needed, e.g., ProviderUnavailableError
        # from debrid.base import ProviderUnavailableError
        # if isinstance(e, ProviderUnavailableError):
        #     return jsonify({'success': False, 'error': f'Provider Error: {str(e)}'}), 503
        return jsonify({'success': False, 'error': error_msg}), 500

@debug_bp.route('/api/direct_emby_scan', methods=['POST'])
@admin_required
def direct_emby_scan():
    """Triggers a full scan of Emby/Jellyfin and adds items to the database."""
    from utilities.emby_functions import get_collected_from_emby
    from database.collected_items import add_collected_items
    logging.info("Received request for direct Emby/Jellyfin scan and collection.")
    try:
        # 1. Get collected items from Emby/Jellyfin
        logging.info("Starting Emby/Jellyfin collection...")
        collected_data = get_collected_from_emby(bypass=True) # bypass=True to scan all configured libs

        if collected_data is None:
            logging.error("Failed to retrieve data from Emby/Jellyfin.")
            return jsonify({'success': False, 'error': 'Failed to retrieve data from Emby/Jellyfin.'}), 500

        movies = collected_data.get('movies', [])
        episodes = collected_data.get('episodes', [])
        combined_items = movies + episodes

        logging.info(f"Retrieved {len(movies)} movies and {len(episodes)} episodes from Emby/Jellyfin.")

        if not combined_items:
             logging.warning("No items collected from Emby/Jellyfin scan.")
             return jsonify({'success': True, 'message': 'No items collected from Emby/Jellyfin scan.'}), 200

        # 2. Add collected items to the database
        logging.info("Adding collected Emby/Jellyfin items to the database...")
        add_collected_items(combined_items, recent=True) # Change to recent=True for additive only
        logging.info("Successfully added Emby/Jellyfin items to the database.")

        return jsonify({'success': True, 'message': f'Successfully processed {len(combined_items)} items from Emby/Jellyfin.'}), 200

    except Exception as e:
        logging.error(f"Error during direct Emby/Jellyfin scan: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': f'An error occurred: {str(e)}'}), 500

# --- New route to delete cache files ---
@debug_bp.route('/api/delete_cache_files', methods=['POST'])
@admin_required
def delete_cache_files_route():
    """API endpoint to delete selected cache files."""
    selected_files = request.form.getlist('selected_files') # Get list of filenames from form
    if not selected_files:
        return jsonify({'success': False, 'error': 'No cache files selected'}), 400

    db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    deleted_count = 0
    errors = []

    for filename in selected_files:
        # Basic validation to prevent deleting unintended files
        if not (filename.startswith('content_source_') and filename.endswith('_cache.pkl')):
            errors.append(f"Invalid cache filename skipped: {filename}")
            continue
            
        file_path = os.path.join(db_content_dir, filename)
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                deleted_count += 1
                logging.info(f"Deleted cache file: {file_path}")
            else:
                logging.warning(f"Cache file not found, skipping deletion: {file_path}")
        except OSError as e:
            logging.error(f"Error deleting cache file {file_path}: {e}")
            errors.append(f"Failed to delete {filename}: {e.strerror}")
        except Exception as e:
            logging.error(f"Unexpected error deleting cache file {file_path}: {e}")
            errors.append(f"Failed to delete {filename}: {str(e)}")

    if not errors:
        return jsonify({'success': True, 'message': f'Successfully deleted {deleted_count} cache file(s).'})
    else:
        error_message = f'Deleted {deleted_count} cache file(s). Errors encountered: {"; ".join(errors)}'
        # Return success=True even with partial failures, but include error details
        return jsonify({'success': True, 'message': error_message, 'errors': errors})
# --- End new route ---

# --- Symlink Recovery Routes ---

@debug_bp.route('/recover_symlinks')
@admin_required
def recover_symlinks_page():
    """Renders the symlink recovery page."""
    return render_template('recover_symlinks.html')

def parse_symlink(symlink_path: Path):
    """Parses a symlink path based on filename patterns, not templates."""
    filename = symlink_path.name
    parsed_data = {
        'symlink_path': str(symlink_path),
        'original_path_for_symlink': None, # Populated in analyze_symlinks
        'media_type': None, # Determined below
        'imdb_id': None, # Determined below
        'tmdb_id': None, # Populated by get_metadata
        'title': None, # Populated by get_metadata
        'year': None, # Populated by get_metadata
        'season_number': None, # Determined below
        'episode_number': None, # Determined below
        'episode_title': None, # Populated by get_metadata
        'version': None, # Populated by reverse_parser in analyze_symlinks
        'original_filename': None, # Populated in analyze_symlinks
        'is_anime': False # Populated by get_metadata
    }

    # 1. Extract IMDb ID (tt#######)
    imdb_match = re.search(r'(tt\d{7,})', filename, re.IGNORECASE)
    if imdb_match:
        parsed_data['imdb_id'] = imdb_match.group(1)
    else:
        logging.warning(f"Could not extract IMDb ID from filename: {filename}")
        return None # Cannot proceed without IMDb ID

    # 2. Extract Season and Episode Numbers (S##E## or similar)
    # More robust regex to handle variations like S01E01, S1E1, Season 1 Episode 1, 1x01 etc.
    se_match = re.search(r'[Ss](\d{1,2})[EeXx](\d{1,3})|Season\s?(\d{1,2})\s?Episode\s?(\d{1,3})|(\d{1,2})[Xx](\d{1,3})', filename)
    if se_match:
        parsed_data['media_type'] = 'episode'
        # Extract numbers from the first matching group that isn't None
        if se_match.group(1) is not None and se_match.group(2) is not None:
            parsed_data['season_number'] = int(se_match.group(1))
            parsed_data['episode_number'] = int(se_match.group(2))
        elif se_match.group(3) is not None and se_match.group(4) is not None:
             parsed_data['season_number'] = int(se_match.group(3))
             parsed_data['episode_number'] = int(se_match.group(4))
        elif se_match.group(5) is not None and se_match.group(6) is not None:
             parsed_data['season_number'] = int(se_match.group(5))
             parsed_data['episode_number'] = int(se_match.group(6))
        else:
             logging.warning(f"Regex matched S/E pattern but failed to extract numbers for: {filename}")
             # Decide if this is fatal? Maybe still treat as movie?
             parsed_data['media_type'] = 'movie' # Fallback to movie if numbers aren't extracted
    else:
        parsed_data['media_type'] = 'movie'

    logging.debug(f"Parsed initial data from {filename}: IMDb={parsed_data['imdb_id']}, Type={parsed_data['media_type']}, S={parsed_data['season_number']}, E={parsed_data['episode_number']}")
    return parsed_data

def _run_analysis_thread(symlink_root_path_str, original_root_path_str, task_id):
    """The actual analysis logic, run in a background thread."""
    global analysis_progress

    # --- Create a temporary directory for recovery files ---
    db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    temp_recovery_dir = os.path.join(db_content_dir, 'tmp_recovery')
    try:
        os.makedirs(temp_recovery_dir, exist_ok=True)
    except OSError as e:
        logging.error(f"Failed to create temporary recovery directory {temp_recovery_dir}: {e}")
        # Handle error: update progress and exit thread? For now, log and continue, recovery will fail later.
        pass 
    # --- End temporary directory creation ---

    # Generate a unique temporary file path for this task
    recovery_file_path = os.path.join(temp_recovery_dir, f"recovery_{task_id}.jsonl")

    analysis_progress[task_id] = {
        'status': 'starting',
        'message': 'Initializing analysis...',
        'total_items_scanned': 0,
        'total_symlinks_processed': 0,
        'total_files_processed': 0,
        'items_found': 0,
        'parser_errors': 0,
        'metadata_errors': 0,
        'recoverable_items_preview': [], # Keep the preview list
        # 'recoverable_items': [], # REMOVED - Will use file instead
        'recovery_file_path': None, # Will be set on completion
        'complete': False
    }

    def update_progress(**kwargs):
        if task_id in analysis_progress:
            analysis_progress[task_id].update(kwargs)
            # Limit the preview list size
            preview = analysis_progress[task_id]['recoverable_items_preview']
            if len(preview) > 5:
                 analysis_progress[task_id]['recoverable_items_preview'] = preview[:5]
        else:
            logging.warning(f"Task ID {task_id} not found in progress dict during update.")

    # Use a try/finally block to ensure file closure and cleanup logic
    recovery_file = None
    try:
        # Open the recovery file in append mode with UTF-8 encoding
        recovery_file = open(recovery_file_path, 'a', encoding='utf-8')

        symlink_root_path = Path(symlink_root_path_str)
        original_root_path = Path(original_root_path_str) if original_root_path_str else None

        if not symlink_root_path.is_dir():
            raise ValueError('Symlink Root Path must be a valid directory.')
        if original_root_path and not original_root_path.is_dir():
            raise ValueError('Original Root Path must be valid if provided.')

        # Read relevant settings
        symlink_folder_order_str = get_setting('File Management', 'symlink_folder_order', 'type,version,resolution')
        organize_by_type = get_setting('File Management', 'symlink_organize_by_type', True)
        organize_by_resolution = get_setting('File Management', 'symlink_organize_by_resolution', False)
        organize_by_version = get_setting('File Management', 'symlink_organize_by_version', False)
        
        separate_anime = get_setting('Debug', 'enable_separate_anime_folders', False)
        movies_folder_name = get_setting('Debug', 'movies_folder_name', 'Movies')
        tv_shows_folder_name = get_setting('Debug', 'tv_shows_folder_name', 'TV Shows')
        anime_movies_folder_name = get_setting('Debug', 'anime_movies_folder_name', 'Anime Movies')
        anime_tv_shows_folder_name = get_setting('Debug', 'anime_tv_shows_folder_name', 'Anime TV Shows')

        ignored_extensions = {'.srt', '.sub', '.idx', '.nfo', '.txt', '.jpg', '.png', '.db', '.partial', '.!qB'}
        
        folder_order_components = [comp.strip() for comp in symlink_folder_order_str.split(',')]
        component_map = {'type': [], 'resolution': [], 'version': []}

        if organize_by_type:
            if movies_folder_name: component_map['type'].append(movies_folder_name)
            if tv_shows_folder_name: component_map['type'].append(tv_shows_folder_name)
            if separate_anime:
                if anime_movies_folder_name: component_map['type'].append(anime_movies_folder_name)
                if anime_tv_shows_folder_name: component_map['type'].append(anime_tv_shows_folder_name)
        
        if organize_by_resolution:
            # Consistent with original code's typical resolution folder names
            component_map['resolution'] = ["2160p", "1080p", "720p", "SD"] 

        if organize_by_version:
            all_settings = get_all_settings() # Use get_all_settings to fetch nested dict
            scraping_settings = all_settings.get('Scraping', {})
            version_configs = scraping_settings.get('versions', {})
            configured_versions = [str(v).strip('*') for v in version_configs.keys() if str(v).strip('*')]
            if configured_versions:
                component_map['version'] = configured_versions
            else:
                component_map['version'].append("Default")


        paths_to_scan_tuples = [(symlink_root_path, symlink_root_path.name)] # (Path, description_for_logging)

        for component_key in folder_order_components:
            folders_for_this_component_type = component_map.get(component_key, [])
            
            is_component_active_for_path_building = False
            if component_key == 'type' and organize_by_type: is_component_active_for_path_building = True
            elif component_key == 'resolution' and organize_by_resolution: is_component_active_for_path_building = True
            elif component_key == 'version' and organize_by_version: is_component_active_for_path_building = True

            if is_component_active_for_path_building and folders_for_this_component_type:
                current_level_new_paths = []
                for base_path_obj, base_desc_str in paths_to_scan_tuples:
                    for folder_segment_name in folders_for_this_component_type:
                        if folder_segment_name: # Ensure folder_segment_name is not None or empty
                            current_level_new_paths.append(
                                (base_path_obj / folder_segment_name, f"{base_desc_str}/{folder_segment_name}")
                            )
                if current_level_new_paths: 
                    paths_to_scan_tuples = current_level_new_paths
        
        total_items_scanned = 0
        total_symlinks_processed = 0
        total_files_processed = 0
        items_found = 0
        parser_errors = 0
        metadata_errors = 0
        recoverable_items_preview = []

        update_progress(status='scanning', message='Starting directory scan...')

        for current_search_path, scan_target_name in paths_to_scan_tuples:
            if current_search_path.is_dir():
                update_progress(message=f'Scanning {scan_target_name}...')
                try:
                    # Use rglob to scan recursively within the target directory
                    for item_path in current_search_path.rglob('*'):
                        total_items_scanned += 1
                        if total_items_scanned % 100 == 0: # Update progress periodically
                            update_progress(
                                total_items_scanned=total_items_scanned,
                                total_symlinks_processed=total_symlinks_processed,
                                total_files_processed=total_files_processed,
                                items_found=items_found,
                                parser_errors=parser_errors,
                                metadata_errors=metadata_errors,
                                message=f'Scanned {total_items_scanned} items...'
                                )

                        if item_path.suffix.lower() in ignored_extensions:
                            continue

                        if item_path.is_file() or item_path.is_symlink():
                            if item_path.is_symlink():
                                total_symlinks_processed += 1
                            else:
                                total_files_processed += 1

                            parsed_data = parse_symlink(item_path)
                            if not parsed_data:
                                parser_errors += 1
                                continue # Skip if initial parse fails

                            # --- Determine original path and filename ---
                            original_path_obj = None
                            if item_path.is_symlink():
                                try:
                                    target_path_str = os.readlink(str(item_path))
                                    if not os.path.isabs(target_path_str):
                                        target_path_str = os.path.abspath(os.path.join(item_path.parent, target_path_str))
                                    original_path_obj = Path(target_path_str)
                                except Exception as e:
                                    parsed_data['original_path_for_symlink'] = f"Error: Cannot read link target ({e})"
                                    parsed_data['original_filename'] = item_path.name
                            elif item_path.is_file():
                                original_path_obj = item_path

                            if original_path_obj and original_path_obj.is_file():
                                parsed_data['original_path_for_symlink'] = str(original_path_obj)
                                parsed_data['original_filename'] = original_path_obj.name
                            elif 'original_path_for_symlink' not in parsed_data:
                                if original_path_obj:
                                        parsed_data['original_path_for_symlink'] = f"Error: Target not a file ({original_path_obj})"
                                else:
                                    parsed_data['original_path_for_symlink'] = "Error: Original path unknown"
                                parsed_data['original_filename'] = item_path.name
                            # --- End original path determination ---

                            # --- Get version ---    
                            filename_for_version = parsed_data.get('original_filename')
                            if filename_for_version:
                                try:
                                    version_raw = parse_filename_for_version(filename_for_version)
                                    parsed_data['version'] = version_raw.strip('*') if version_raw else 'Default'
                                except Exception as e:
                                    parsed_data['version'] = 'Default'
                            else:
                                parsed_data['version'] = 'Default'
                            # --- End version --- 
                                
                            # --- Fetch metadata --- 
                            if parsed_data['imdb_id']:
                                metadata_args = {
                                    'imdb_id': parsed_data['imdb_id'],
                                    'item_media_type': parsed_data.get('media_type') # Re-add based on function signature
                                }
                                # Removed conditional adding of season/episode number
                                # get_metadata likely handles this internally based on imdb_id
                                try:
                                    # Pass the original parsed_data as original_item if needed by get_metadata internal logic
                                    metadata_args['original_item'] = parsed_data 
                                    from metadata.metadata import get_metadata
                                    metadata = get_metadata(**metadata_args)
                                    if metadata:
                                        parsed_data['title'] = metadata.get('title')
                                        parsed_data['year'] = metadata.get('year')
                                        # Update tmdb_id if get_metadata found it
                                        parsed_data['tmdb_id'] = metadata.get('tmdb_id') or parsed_data.get('tmdb_id')
                                        parsed_data['release_date'] = metadata.get('release_date')
                                        if parsed_data['media_type'] == 'episode':
                                            parsed_data['episode_title'] = metadata.get('episode_title')
                                        genres = metadata.get('genres', [])
                                        if isinstance(genres, list):
                                            parsed_data['is_anime'] = any(g.lower() in ['animation', 'anime'] for g in genres)
                                        
                                        items_found += 1
                                        
                                        # --- Write item to recovery file ---
                                        try:
                                            recovery_file.write(json.dumps(parsed_data) + '\n')
                                        except Exception as write_err:
                                            logging.error(f"Error writing item to recovery file {recovery_file_path}: {write_err}")
                                            # Maybe mark the task as failed? For now, log and continue.
                                            # update_progress(status='error', message=f'Error writing recovery file: {write_err}')
                                        # --- End write item ---

                                        # Update preview list for UI feedback during scan
                                        if len(recoverable_items_preview) < 5:
                                                recoverable_items_preview.append(parsed_data)
                                        
                                        # Update progress (only preview list is stored in memory now)
                                        update_progress(items_found=items_found, recoverable_items_preview=recoverable_items_preview)
                                    else:
                                        metadata_errors += 1
                                except Exception as e:
                                    logging.error(f"Metadata error for {metadata_args}: {e}", exc_info=False) # Less verbose logging
                                    metadata_errors += 1
                        else: # No IMDb ID was parsed
                            parser_errors += 1 # This case should be caught by parse_symlink returning None now
                            logging.warning(f"Skipping {item_path.name} as no IMDb ID was parsed (should have been caught earlier).")

                except Exception as e_rglob:
                    logging.error(f"Error during rglob scan of {current_search_path}: {e_rglob}", exc_info=True)
            else:
                logging.warning(f"Directory not found or not accessible: {current_search_path} (derived from order: {symlink_folder_order_str})")
                    
        # Analysis complete, update final status and store recovery file path
        update_progress(
            status='complete',
            message='Analysis finished.',
            complete=True,
            # recoverable_items=analysis_progress[task_id]['recoverable_items'], # REMOVED
            recovery_file_path=recovery_file_path if items_found > 0 else None, # Store file path if items were found
            total_items_scanned=total_items_scanned,
            total_symlinks_processed=total_symlinks_processed,
            total_files_processed=total_files_processed,
            items_found=items_found,
            parser_errors=parser_errors,
            metadata_errors=metadata_errors
        )

    except Exception as e:
        logging.error(f"Analysis thread error for task {task_id}: {e}", exc_info=True)
        update_progress(status='error', message=f'Analysis failed: {e}', complete=True)
    finally:
        if recovery_file:
            try:
                recovery_file.close()
            except Exception as close_err:
                 logging.error(f"Error closing recovery file {recovery_file_path}: {close_err}")
        pass

@debug_bp.route('/analyze_symlinks', methods=['POST'])
@admin_required
def analyze_symlinks():
    """Initiates the symlink analysis in a background thread and returns a task ID."""
    import uuid
    symlink_root_path_str = request.form.get('symlink_root_path')
    original_root_path_str = request.form.get('original_root_path')

    if not symlink_root_path_str:
        return jsonify({'success': False, 'error': 'Symlink Root Path is required.'}), 400

    task_id = str(uuid.uuid4())
    
    # Start analysis in background thread
    thread = threading.Thread(
        target=_run_analysis_thread, 
        args=(symlink_root_path_str, original_root_path_str, task_id)
    )
    thread.daemon = True
    thread.start()

    return jsonify({'success': True, 'task_id': task_id}) # Return task ID for progress tracking

@debug_bp.route('/analysis_progress/<task_id>')
def analysis_progress_stream(task_id):
    """SSE endpoint for tracking analysis progress."""
    def generate():
        while True:
            if task_id not in analysis_progress:
                progress = {'status': 'error', 'message': 'Task not found or expired', 'complete': True}
                yield f"data: {json.dumps(progress)}\n\n"
                break
                
            progress = analysis_progress[task_id]
            yield f"data: {json.dumps(progress)}\n\n"
            
            if progress.get('complete', False):
                # Maybe remove from dict after sending final status?
                # analysis_progress.pop(task_id, None) 
                break
                
            time.sleep(1) # Poll interval
            
    return Response(stream_with_context(generate()), mimetype='text/event-stream')

@debug_bp.route('/perform_recovery', methods=['POST'])
@admin_required
def perform_recovery():
    """Recovers all items found during a specific analysis task by reading from its recovery file."""
    from database import add_media_item

    data = request.get_json()
    task_id = data.get('task_id')

    if not task_id:
        return jsonify({'success': False, 'error': 'Missing task_id.'}), 400

    # Retrieve the analysis results (contains the file path)
    if task_id not in analysis_progress or not analysis_progress[task_id].get('complete'):
        return jsonify({'success': False, 'error': f'Analysis task {task_id} not found or not complete.'}), 404

    analysis_result = analysis_progress[task_id]
    recovery_file_path = analysis_result.get('recovery_file_path')
    expected_items = analysis_result.get('items_found', 0) # Get expected count

    if not recovery_file_path:
        # Check if items_found was 0, meaning no file was expected
        if expected_items == 0:
             return jsonify({'success': True, 'message': 'Analysis found no items to recover.', 'successful_recoveries': 0, 'failed_recoveries': 0}), 200
        else:
            return jsonify({'success': False, 'error': f'Recovery file path not found for completed task {task_id}. Analysis might have failed partially?'}), 404

    if not os.path.exists(recovery_file_path):
         return jsonify({'success': False, 'error': f'Recovery file not found at {recovery_file_path}. It might have been deleted or analysis failed.'}), 404

    # Removed: conn = None - add_media_item handles its own connection
    recovery_file = None
    successful_recoveries = 0
    failed_recoveries = 0
    errors = []
    COMMIT_BATCH_SIZE = 500 # Commit every 500 items - Note: add_media_item commits individually

    try:
        # Removed: conn = get_db_connection()
        recovery_file = open(recovery_file_path, 'r', encoding='utf-8')

        logging.info(f"Starting recovery from file: {recovery_file_path} for task {task_id}")

        for line_num, line in enumerate(recovery_file):
            item_data = None # Reset for each line
            try:
                line = line.strip()
                if not line: # Skip empty lines
                    continue

                item_data = json.loads(line)

                now_iso = datetime.now()

                # Prepare data ONLY with valid DB columns
                db_item_for_insert = {
                    'imdb_id': item_data.get('imdb_id'),
                    'tmdb_id': item_data.get('tmdb_id'),
                    'title': item_data.get('title'),
                    'year': item_data.get('year'),
                    'release_date': item_data.get('release_date'),
                    'state': 'Collected', # Mark as collected
                    'type': item_data.get('media_type'), # Source key is 'media_type', DB column is 'type'
                    'season_number': item_data.get('season_number'),
                    'episode_number': item_data.get('episode_number'),
                    'episode_title': item_data.get('episode_title'),
                    'collected_at': now_iso,
                    'original_collected_at': now_iso,
                    'original_path_for_symlink': item_data.get('original_path_for_symlink'),
                    'version': item_data.get('version', 'Default'),
                    'filled_by_file': item_data.get('original_filename'),
                    # 'last_updated': now_iso, # Let add_media_item handle this
                    'metadata_updated': now_iso,
                    'wake_count': 0,
                    # 'attempts': 0, # Removed - Not a DB column
                    # 'is_anime': item_data.get('is_anime', False), # Removed - Not a DB column (trigger_is_anime exists but not populated here)
                    'location_on_disk': item_data.get('symlink_path')
                    # Note: 'manually_added' and 'date_added' were also removed as they are not DB columns
                }

                # Filter out None values before passing to db function
                db_item_filtered = {k: v for k, v in db_item_for_insert.items() if v is not None}

                # Validate essential keys after filtering
                if not db_item_filtered.get('imdb_id') or not db_item_filtered.get('type'):
                    raise ValueError(f"Missing essential data (imdb_id or type) after filtering")

                # Call add_media_item and handle potential IntegrityError
                try:
                    # Pass the explicitly constructed and filtered dictionary
                    item_id = add_media_item(db_item_filtered)
                    if item_id:
                        successful_recoveries += 1
                    else:
                        # add_media_item returning None suggests an issue other than IntegrityError
                        raise Exception("add_media_item failed to return an ID")
                except sqlite3.IntegrityError:
                     failed_recoveries += 1
                     item_desc = f"item on line {line_num + 1} (Path: {item_data.get('symlink_path', 'Unknown')})"
                     error_msg = f"Skipped recovery for {item_desc}: Item likely already exists (UNIQUE constraint violation)."
                     errors.append(error_msg)
                     logging.warning(error_msg) # Log as warning, not error

            except json.JSONDecodeError as json_err:
                 failed_recoveries += 1
                 error_msg = f"Failed to parse JSON on line {line_num + 1}: {json_err}"
                 errors.append(error_msg)
                 logging.error(error_msg)
            except ValueError as val_err:
                failed_recoveries += 1
                item_desc = f"item on line {line_num + 1} (Path: {item_data.get('symlink_path', 'Unknown') if item_data else 'Unknown'})"
                error_msg = f"Validation error for {item_desc}: {val_err}"
                errors.append(error_msg)
                logging.error(error_msg)
            except Exception as e:
                failed_recoveries += 1
                item_desc = f"item on line {line_num + 1} (Path: {item_data.get('symlink_path', 'Unknown') if item_data else 'Unknown'})"
                error_msg = f"Failed to recover {item_desc}: {str(e)}"
                errors.append(error_msg)
                logging.error(error_msg, exc_info=True) # Log full trace for unexpected errors

        # Removed final commit logic
        logging.info(f"Recovery processing complete for task {task_id}. Total successful: {successful_recoveries}, Failed/Skipped: {failed_recoveries}")

    except Exception as outer_err:
        # Error opening file or other outer-level issues
        error_msg = f"Error during recovery process: {str(outer_err)}"
        errors.append(error_msg)
        logging.error(error_msg, exc_info=True)
        # Can't determine success/failure counts accurately here, maybe set failed to expected?
        failed_recoveries = expected_items - successful_recoveries # Estimate failures
    finally:
        if recovery_file:
            try:
                recovery_file.close()
            except Exception as close_err:
                 logging.error(f"Error closing recovery file {recovery_file_path}: {close_err}")
        # Removed: conn close logic

        # Clean up the recovery file only if there were no errors during the file processing/db interaction
        if recovery_file_path and os.path.exists(recovery_file_path) and not errors:
            try:
                os.remove(recovery_file_path)
                logging.info(f"Successfully deleted recovery file: {recovery_file_path}")
            except Exception as del_err:
                logging.error(f"Failed to delete recovery file {recovery_file_path}: {del_err}")
                # Add a note about manual deletion maybe?
                errors.append(f"Note: Failed to automatically delete recovery file {os.path.basename(recovery_file_path)}. Please delete it manually.")
        elif errors:
             logging.warning(f"Recovery file {recovery_file_path} was not deleted due to errors during the recovery process.")
             errors.append(f"Note: Recovery file {os.path.basename(recovery_file_path)} was kept due to errors. Please review and delete it manually.")


    return jsonify({
        'success': failed_recoveries == 0, # Success only if no errors/skips? Or should skipped items be okay? Let's stick with failed_recoveries == 0 for now.
        'successful_recoveries': successful_recoveries,
        'failed_recoveries': failed_recoveries, # Includes skipped items due to IntegrityError
        'errors': errors
    })

# --- End Symlink Recovery Routes ---

# --- Symlink Path Modification --- 
@debug_bp.route('/api/modify_symlink_paths', methods=['POST'])
@admin_required
def modify_symlink_paths():
    """API endpoint to modify base paths for symlinks and original files in the database."""
    current_symlink_base = request.form.get('current_symlink_base', '').strip()
    new_symlink_base = request.form.get('new_symlink_base', '').strip()
    current_original_base = request.form.get('current_original_base', '').strip()
    new_original_base = request.form.get('new_original_base', '').strip()
    dry_run = request.form.get('dry_run') == 'on'

    modify_symlink = bool(current_symlink_base and new_symlink_base)
    modify_original = bool(current_original_base and new_original_base)

    if not modify_symlink and not modify_original:
        return jsonify({'success': False, 'error': 'Please provide at least one pair of current and new base paths to modify.'}), 400

    logging.info(f"Symlink path modification requested. Dry run: {dry_run}")
    if modify_symlink: logging.info(f"  Symlink: '{current_symlink_base}' -> '{new_symlink_base}'")
    if modify_original: logging.info(f"  Original: '{current_original_base}' -> '{new_original_base}'")

    from database import get_db_connection
    conn = None
    items_to_update = []
    preview_items = [] # For dry run
    MAX_PREVIEW = 10

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Query items that might need updating
        query = "SELECT id, location_on_disk, original_path_for_symlink FROM media_items WHERE "
        conditions = []
        params = []
        if modify_symlink:
            conditions.append("location_on_disk LIKE ?")
            params.append(current_symlink_base + '%')
        if modify_original:
            conditions.append("original_path_for_symlink LIKE ?")
            params.append(current_original_base + '%')
        
        query += " OR ".join(conditions)
        cursor.execute(query, params)
        items = cursor.fetchall()

        logging.info(f"Found {len(items)} potentially matching items in the database.")

        for item in items:
            item_id = item['id']
            current_location = item['location_on_disk']
            current_original = item['original_path_for_symlink']
            new_location = current_location
            new_original = current_original
            updated = False

            if modify_symlink and current_location and current_location.startswith(current_symlink_base):
                new_location = current_location.replace(current_symlink_base, new_symlink_base, 1)
                updated = True
                logging.debug(f"Item {item_id}: Symlink change '{current_location}' -> '{new_location}'")

            if modify_original and current_original and current_original.startswith(current_original_base):
                new_original = current_original.replace(current_original_base, new_original_base, 1)
                updated = True
                logging.debug(f"Item {item_id}: Original change '{current_original}' -> '{new_original}'")
            
            if updated:
                update_data = {
                    'id': item_id,
                    'new_location': new_location,
                    'new_original': new_original
                }
                items_to_update.append(update_data)
                if dry_run and len(preview_items) < MAX_PREVIEW:
                    preview_items.append({
                        'id': item_id,
                        'old_location': current_location,
                        'new_location': new_location,
                        'old_original': current_original,
                        'new_original': new_original
                    })

        logging.info(f"Identified {len(items_to_update)} items for potential update.")

        if dry_run:
            return jsonify({
                'success': True,
                'dry_run': True,
                'message': f"Dry run complete. Found {len(items_to_update)} items to update.",
                'items_to_update_count': len(items_to_update),
                'preview': preview_items
            })
        else:
            # Perform actual updates
            updated_count = 0
            if items_to_update:
                update_sql = "UPDATE media_items SET location_on_disk = ?, original_path_for_symlink = ? WHERE id = ?"
                # Prepare data for executemany
                update_params = [
                    (item['new_location'], item['new_original'], item['id'])
                    for item in items_to_update
                ]
                cursor.executemany(update_sql, update_params)
                conn.commit()
                updated_count = cursor.rowcount # Note: executemany rowcount might be unreliable on some drivers/dbs
                # Fetch actual count as fallback 
                if updated_count == -1 or updated_count is None:
                    updated_count = len(items_to_update)
                    
                logging.info(f"Successfully updated {updated_count} items in the database.")
                message = f"Successfully updated {updated_count} items."
            else:
                 message = "No items required updating based on the provided paths."

            return jsonify({
                'success': True,
                'dry_run': False,
                'message': message,
                'updated_count': updated_count
            })

    except Exception as e:
        logging.error(f"Error modifying symlink paths: {e}", exc_info=True)
        if conn and not dry_run: # Rollback if it wasn't a dry run
            try:
                conn.rollback()
            except Exception as rb_err:
                 logging.error(f"Rollback failed: {rb_err}")
        return jsonify({'success': False, 'error': f'An error occurred: {str(e)}'}), 500
    finally:
        if conn:
            conn.close()
# --- End Symlink Path Modification ---

@debug_bp.route('/api/delete_battery_db', methods=['POST'])
@admin_required
def delete_battery_db_files():
    """Deletes the cli_battery.db and associated journal/WAL files."""
    db_content_dir = os.environ.get('USER_DB_CONTENT')
    if not db_content_dir:
        logging.error("USER_DB_CONTENT environment variable not set.")
        return jsonify({'success': False, 'error': 'USER_DB_CONTENT environment variable not set'}), 500

    base_db_path = os.path.join(db_content_dir, 'cli_battery.db')
    files_to_delete = [
        base_db_path,
        base_db_path + '-shm',
        base_db_path + '-wal'
    ]

    deleted_files = []
    errors = []

    for file_path in files_to_delete:
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                deleted_files.append(os.path.basename(file_path))
                logging.info(f"Deleted battery DB file: {file_path}")
            else:
                logging.info(f"Battery DB file not found, skipping: {file_path}")
        except Exception as e:
            error_msg = f"Error deleting file {os.path.basename(file_path)}: {str(e)}"
            logging.error(error_msg)
            errors.append(error_msg)

    if errors:
        message = f'Errors occurred during deletion. Deleted: {", ".join(deleted_files) if deleted_files else "None"}. Errors: {"; ".join(errors)}'
        return jsonify({'success': False, 'error': message}), 500
    elif not deleted_files:
         return jsonify({'success': True, 'message': 'No battery DB files found to delete.'}), 200
    else:
        return jsonify({'success': True, 'message': f'Successfully deleted files: {", ".join(deleted_files)}'}), 200

# --- Rclone Mount to Symlinks Logic ---

def _run_rclone_to_symlink_task(rclone_mount_path_str, symlink_base_path_str, dry_run, task_id, trigger_plex_update_on_success: bool = False, assumed_item_title_from_path: str = None): # Add new parameter
    """Background task to scan Rclone mount, fetch metadata, and create DB entries/symlinks."""
    global rclone_scan_progress

    # --- Progress File Setup ---
    db_content_dir = os.environ.get('USER_DB_CONTENT')
    if not db_content_dir:
        # Fallback if env var is not set (should not happen if main.py runs first)
        logging.error(f"[RcloneScan {task_id}] USER_DB_CONTENT environment variable not found. Progress persistence will be disabled for this run.")
        progress_file_path = None 
    else:
        progress_file_path = os.path.join(db_content_dir, 'rclone_to_symlink_processed_files.json')
        progress_file_path = None # REMOVED

    processed_original_files = set()
    if progress_file_path:
        try:
            if os.path.exists(progress_file_path):
                with open(progress_file_path, 'r') as f:
                    processed_original_files = set(json.load(f))
                logging.info(f"[RcloneScan {task_id}] Loaded {len(processed_original_files)} previously processed file paths.")
        except (json.JSONDecodeError, OSError) as e:
            logging.warning(f"[RcloneScan {task_id}] Could not load progress file {progress_file_path}: {e}. Starting with empty progress.")
            processed_original_files = set()

    def save_rclone_progress():
        if progress_file_path:
            try:
                with open(progress_file_path, 'w') as f:
                    json.dump(list(processed_original_files), f, indent=2)
            except OSError as e:
                logging.error(f"[RcloneScan {task_id}] Could not save progress file {progress_file_path}: {e}")
    # --- End Progress File Setup ---

    MOVIE_SIZE_THRESHOLD_BYTES = 300 * 1024 * 1024 # 300 MB

    rclone_scan_progress[task_id] = {
        'status': 'starting',
        'message': 'Initializing Rclone scan...',
        'total_files_scanned': 0,
        'media_files_found': 0,
        'items_processed': 0,
        'items_added_to_db': 0,
        'symlinks_created': 0,
        'parser_errors': 0,
        'metadata_errors': 0,
        'db_errors': 0,
        'symlink_errors': 0,
        'skipped_duplicates': 0,
        'skipped_due_to_size': 0, # New counter
        'skipped_previously_processed': 0, # New counter
        'preview': [], 
        'errors': [], 
        'complete': False
    }

    # REMOVED: get_largest_video_file_in_folder helper function

    skipped_previously_processed_count = 0 # Local counter
    skipped_due_to_size_count = 0 # Local counter for size skips

    def update_progress(**kwargs):
        if task_id in rclone_scan_progress:
            progress_data = rclone_scan_progress[task_id]
            progress_data.update(kwargs)
            if 'preview' in progress_data and len(progress_data['preview']) > 5:
                 progress_data['preview'] = progress_data['preview'][:5]
        else:
            logging.warning(f"Rclone scan Task ID {task_id} not found in progress dict during update.")

    try:
        rclone_mount_path = Path(rclone_mount_path_str)
        symlink_base_path_setting_backup = get_setting('File Management', 'symlinked_files_path')

        # Check if rclone_mount_path is a directory
        if not rclone_mount_path.is_dir():
            raise ValueError(f"Rclone Mount Path is not a valid directory: {rclone_mount_path_str}")

        if not symlink_base_path_str:
             raise ValueError("Symlink Base Path cannot be empty.")
        
        logging.info(f"[RcloneScan {task_id}] Temporarily setting symlink path to: {symlink_base_path_str}")
        try:
             set_setting('File Management', 'symlinked_files_path', symlink_base_path_str)
        except Exception as set_setting_err:
             raise RuntimeError(f"Failed to temporarily set symlink base path: {set_setting_err}")

        update_progress(status='scanning', message='Scanning Rclone mount path...')
        video_extensions = {'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm', '.mpeg', '.mpg'}
        total_files_scanned = 0
        media_files_found = 0
        items_processed = 0
        items_added_to_db = 0
        symlinks_created = 0
        parser_errors = 0
        metadata_errors = 0
        db_errors = 0
        symlink_errors = 0
        skipped_duplicates = 0
        # REMOVED: skipped_smaller_movies_in_folder_count = 0
        preview_list = []
        error_list = []
        direct_api = DirectAPI()

        for item_path in rclone_mount_path.rglob('*'):
            total_files_scanned += 1
            if total_files_scanned % 100 == 0:
                 update_progress(total_files_scanned=total_files_scanned, message=f'Scanned {total_files_scanned} files...')

            if not (item_path.is_file() and item_path.suffix.lower() in video_extensions):
                continue
            
            original_file_path_str = str(item_path) # Used for progress tracking

            # Check if this specific file was already processed and recorded
            if original_file_path_str in processed_original_files:
                logging.info(f"[RcloneScan {task_id}] Skipping previously processed file: {item_path.name}")
                skipped_previously_processed_count += 1
                update_progress(skipped_previously_processed=skipped_previously_processed_count)
                continue

            # REMOVED: Logic for files_to_skip_in_movie_folders
            
            media_files_found += 1
            logging.debug(f"[RcloneScan {task_id}] Evaluating media file: {original_file_path_str}")

            # --- Start of merged parsing logic ---
            parsed_info_folder = {}
            parsed_version_folder = None
            folder_name = item_path.parent.name
            if folder_name:
                try:
                    parsed_info_folder = parse_with_ptt(folder_name)
                    if parsed_info_folder.get('parsing_error'): parsed_info_folder = {}
                    parsed_version_folder = parse_filename_for_version(folder_name)
                except Exception: parsed_info_folder, parsed_version_folder = {}, None
            
            parsed_info_file, parsed_version_file = {}, None
            try:
                parsed_info_file = parse_with_ptt(item_path.name)
                if parsed_info_file.get('parsing_error'): logging.warning(f"[RcloneScan {task_id}] PTT filename parse error for {item_path.name}")
                parsed_version_file = parse_filename_for_version(item_path.name)
            except Exception as e:
                logging.error(f"[RcloneScan {task_id}] PTT/Reverse filename parse failed for {item_path.name}: {e}. Skipping.")
                parser_errors += 1; update_progress(parser_errors=parser_errors); continue

            def get_prioritized_value(key, from_folder, from_file, default=None):
                folder_val = from_folder.get(key)
                file_val = from_file.get(key)
                is_folder_val_empty = folder_val is None or (isinstance(folder_val, str) and not folder_val.strip())
                is_file_val_empty = file_val is None or (isinstance(file_val, str) and not file_val.strip())
                if not is_folder_val_empty: return folder_val
                if not is_file_val_empty: return file_val
                return default

            parsed_title = get_prioritized_value('title', parsed_info_folder, parsed_info_file)
            parsed_year = get_prioritized_value('year', parsed_info_folder, parsed_info_file)
            parsed_season_folder_val, parsed_season_file_val = parsed_info_folder.get('season'), parsed_info_file.get('season')
            parsed_episode_folder_val, parsed_episode_file_val = parsed_info_folder.get('episode'), parsed_info_file.get('episode')
            parsed_season = parsed_season_folder_val if parsed_season_folder_val is not None else parsed_season_file_val
            parsed_episode = parsed_episode_folder_val if parsed_episode_folder_val is not None else parsed_episode_file_val
            if isinstance(parsed_season, list) and parsed_season: parsed_season = parsed_season[0]
            if isinstance(parsed_episode, list) and parsed_episode: parsed_episode = parsed_episode[0]

            is_version_folder_empty = parsed_version_folder is None or not str(parsed_version_folder).strip()
            is_version_file_empty = parsed_version_file is None or not str(parsed_version_file).strip()
            current_parsed_version = 'Default'
            if not is_version_folder_empty: current_parsed_version = str(parsed_version_folder)
            elif not is_version_file_empty: current_parsed_version = str(parsed_version_file)
            current_parsed_version = current_parsed_version.strip('*') if current_parsed_version else 'Default'
            if not current_parsed_version: current_parsed_version = 'Default'

            current_parsed_type = 'episode' if parsed_season is not None or parsed_episode is not None else 'movie'
            if not parsed_title:
                logging.warning(f"[RcloneScan {task_id}] No title for {item_path.name}. Skipping.")
                parser_errors += 1; update_progress(parser_errors=parser_errors); continue
            # --- End of merged parsing logic ---

            # --- Movie: Size check logic ---
            if current_parsed_type == 'movie':
                try:
                    file_size = item_path.stat().st_size
                    if file_size < MOVIE_SIZE_THRESHOLD_BYTES:
                        logging.info(f"[RcloneScan {task_id}] Skipping movie '{item_path.name}' due to size ({file_size / (1024*1024):.2f}MB) below threshold ({MOVIE_SIZE_THRESHOLD_BYTES / (1024*1024):.2f}MB).")
                        skipped_due_to_size_count += 1
                        update_progress(skipped_due_to_size=skipped_due_to_size_count)
                        continue 
                except OSError as e:
                    logging.warning(f"[RcloneScan {task_id}] Could not get stats for movie file {item_path.name}: {e}. Skipping.")
                    # parser_errors += 1 # Or a new counter for stat errors
                    # update_progress(parser_errors=parser_errors)
                    continue
            # --- End Movie Size Check Logic ---
            
            update_progress(message=f'Processing: {item_path.name} ({current_parsed_type})')
            items_processed += 1

            # 2. Fetch Metadata
            metadata = None
            final_imdb_id, final_tmdb_id = None, None
            try:
                item_id_to_use = None
                search_type_for_api = 'show' if current_parsed_type == 'episode' else 'movie'
                
                # Determine titles from filename and folder
                filename_title_raw = parsed_info_file.get('title')
                cleaned_filename_title = filename_title_raw.replace('.', ' ') if filename_title_raw and filename_title_raw.strip() else None

                folder_title_raw = parsed_info_folder.get('title')
                cleaned_folder_title = folder_title_raw.replace('.', ' ') if folder_title_raw and folder_title_raw.strip() else None

                final_search_results = None
                title_that_yielded_search_results = None

                # Attempt 1: Search using cleaned filename title
                if cleaned_filename_title:
                    logging.info(f"[RcloneScan {task_id}] Attempting primary search with filename title: '{cleaned_filename_title}', Year='{parsed_year}', Type='{search_type_for_api}' for File='{item_path.name}'")
                    search_results_file, _ = direct_api.search_media(query=cleaned_filename_title, year=parsed_year, media_type=search_type_for_api)
                    if search_results_file:
                        final_search_results = search_results_file
                        title_that_yielded_search_results = cleaned_filename_title
                        logging.info(f"[RcloneScan {task_id}] Primary search with filename title '{cleaned_filename_title}' was successful.")
                    else:
                        logging.warning(f"[RcloneScan {task_id}] Primary search with filename title '{cleaned_filename_title}' yielded no results.")
                else:
                    logging.debug(f"[RcloneScan {task_id}] No valid cleaned filename title to attempt primary search.")

                # Attempt 2: If primary search failed or wasn't possible, try folder title (if different and valid)
                if not final_search_results and cleaned_folder_title and cleaned_folder_title != cleaned_filename_title:
                    logging.info(f"[RcloneScan {task_id}] Attempting fallback search with folder title: '{cleaned_folder_title}', Year='{parsed_year}', Type='{search_type_for_api}' for File='{item_path.name}'")
                    search_results_folder, _ = direct_api.search_media(query=cleaned_folder_title, year=parsed_year, media_type=search_type_for_api)
                    if search_results_folder:
                        final_search_results = search_results_folder
                        title_that_yielded_search_results = cleaned_folder_title
                        logging.info(f"[RcloneScan {task_id}] Fallback search with folder title '{cleaned_folder_title}' was successful.")
                    else:
                        logging.warning(f"[RcloneScan {task_id}] Fallback search with folder title '{cleaned_folder_title}' also yielded no results.")
                elif not final_search_results and cleaned_folder_title and cleaned_folder_title == cleaned_filename_title:
                    logging.debug(f"[RcloneScan {task_id}] Folder title is same as filename title, already attempted or filename title was null; no separate folder title search needed.")
                
                if not final_search_results:
                    logging.warning(f"[RcloneScan {task_id}] All search attempts failed for file '{item_path.name}'. Parsed folder title: '{cleaned_folder_title}', Parsed filename title: '{cleaned_filename_title}'.")

                # Determine the best title to use for find_best_match_from_results scoring (prioritize filename)
                if cleaned_filename_title:
                    title_for_best_match_selection = cleaned_filename_title
                    logging.debug(f"[RcloneScan {task_id}] Using cleaned filename title for best_match selection: '{title_for_best_match_selection}'")
                elif cleaned_folder_title: # Fallback to folder title if filename title was not usable
                    title_for_best_match_selection = cleaned_folder_title
                    logging.debug(f"[RcloneScan {task_id}] Filename title not suitable, falling back to folder title for best_match selection: '{title_for_best_match_selection}'")
                else: # Should ideally not happen if PTT parsed something for `parsed_title`
                    title_for_best_match_selection = parsed_title # Raw PTT output as last resort
                    logging.debug(f"[RcloneScan {task_id}] No suitable cleaned filename or folder title, falling back to raw parsed_title for best_match selection: '{title_for_best_match_selection}'")
                
                best_match_from_search = None
                if final_search_results:
                    from cli_battery.app.metadata_manager import MetadataManager 
                    best_match_from_search = MetadataManager.find_best_match_from_results(
                        original_query_title=title_for_best_match_selection, 
                        query_year=parsed_year,
                        search_results=final_search_results
                    )
                
                if best_match_from_search:
                    logging.info(f"[RcloneScan {task_id}] Best match selected by find_best_match_from_results: {best_match_from_search.get('title')} ({best_match_from_search.get('year')}) using matching title '{title_for_best_match_selection}' (search performed with '{title_that_yielded_search_results}')")
                    item_id_to_use = best_match_from_search.get('imdb_id') or best_match_from_search.get('tmdb_id')
                elif final_search_results: 
                    logging.warning(f"[RcloneScan {task_id}] No confident match from find_best_match_from_results using matching title '{title_for_best_match_selection}'. Falling back to first search result (search performed with '{title_that_yielded_search_results}').")
                    first_match_fallback = final_search_results[0]
                    item_id_to_use = first_match_fallback.get('imdb_id') or first_match_fallback.get('tmdb_id')
                else: 
                    logging.warning(f"[RcloneScan {task_id}] No search results found for file '{item_path.name}' after all attempts to feed into find_best_match_from_results.")
                    item_id_to_use = None

                if item_id_to_use:
                    is_imdb = str(item_id_to_use).startswith('tt')
                    if current_parsed_type == 'movie':
                        imdb_to_fetch_with = None
                        tmdb_known_from_search = None
                        if is_imdb:
                            imdb_to_fetch_with = item_id_to_use
                        else: 
                            tmdb_known_from_search = item_id_to_use # item_id_to_use is TMDB ID string
                            converted_imdb, _ = direct_api.tmdb_to_imdb(tmdb_known_from_search, 'movie')
                            if converted_imdb and str(converted_imdb).strip():
                                imdb_to_fetch_with = str(converted_imdb).strip()
                        
                        if imdb_to_fetch_with:
                            metadata_result, _ = direct_api.get_movie_metadata(imdb_id=imdb_to_fetch_with)
                            if isinstance(metadata_result, dict): # Log keys if it's a dict
                                logging.info(f"[RcloneScan {task_id}] Movie metadata_result keys for IMDb {imdb_to_fetch_with}: {list(metadata_result.keys())}")
                            if metadata_result and isinstance(metadata_result, dict):
                                metadata = metadata_result
                                final_imdb_id = str(metadata.get('imdb_id')).strip() if metadata.get('imdb_id') and str(metadata.get('imdb_id')).strip() else imdb_to_fetch_with
                                # Corrected TMDB ID extraction for movies
                                final_tmdb_id = str(metadata.get('ids', {}).get('tmdb')).strip() if metadata.get('ids', {}).get('tmdb') else tmdb_known_from_search
                            else: 
                                logging.warning(f"[RcloneScan {task_id}] get_movie_metadata for {imdb_to_fetch_with} returned invalid. Using known IDs.")
                                final_imdb_id = imdb_to_fetch_with
                                final_tmdb_id = tmdb_known_from_search
                                metadata = None 
                        elif tmdb_known_from_search: 
                            logging.warning(f"[RcloneScan {task_id}] Only TMDB ID {tmdb_known_from_search} for movie '{parsed_title}'. No IMDb fetch.")
                            final_tmdb_id = tmdb_known_from_search
                            metadata = {'title': parsed_title, 'year': parsed_year, 'id': final_tmdb_id}
                        # If neither imdb_to_fetch_with nor tmdb_known_from_search, IDs remain None.

                    elif current_parsed_type == 'episode':
                        s_num_int, e_num_int = None, None
                        try:
                            if parsed_season is not None: s_num_int = int(parsed_season)
                        except (ValueError, TypeError):
                            logging.warning(f"[RcloneScan {task_id}] Could not convert parsed_season '{parsed_season}' to int for {parsed_title}")
                        try:
                            if parsed_episode is not None: e_num_int = int(parsed_episode)
                        except (ValueError, TypeError):
                            logging.warning(f"[RcloneScan {task_id}] Could not convert parsed_episode '{parsed_episode}' to int for {parsed_title} S{s_num_int}")

                        if s_num_int is None or e_num_int is None:
                            logging.warning(f"[RcloneScan {task_id}] Missing or invalid S ({s_num_int}) or E ({e_num_int}) number for episode-type '{parsed_title}'. Cannot fetch specific episode metadata.")
                            # Keep existing final_imdb_id/final_tmdb_id if they were show IDs from search, but episode metadata is None
                            # This will be caught by 'if not metadata:' later.
                            # To ensure final_imdb_id and final_tmdb_id are set if show search was successful:
                            if is_imdb: final_imdb_id = item_id_to_use
                            else: final_tmdb_id = item_id_to_use # if show search gave TMDB
                        else:
                            # Both s_num_int and e_num_int are valid integers here
                            show_imdb_to_fetch_with = None
                            show_tmdb_known_from_search = None
                            if is_imdb: 
                                show_imdb_to_fetch_with = item_id_to_use
                            else: 
                                show_tmdb_known_from_search = item_id_to_use
                                converted_imdb, _ = direct_api.tmdb_to_imdb(show_tmdb_known_from_search, 'show')
                                if converted_imdb and str(converted_imdb).strip():
                                    show_imdb_to_fetch_with = str(converted_imdb).strip()
                                    
                            if show_imdb_to_fetch_with:
                                show_meta_full, _ = direct_api.get_show_metadata(imdb_id=show_imdb_to_fetch_with)
                                if isinstance(show_meta_full, dict): # Log keys if it's a dict
                                    logging.info(f"[RcloneScan {task_id}] Show show_meta_full keys for IMDb {show_imdb_to_fetch_with}: {list(show_meta_full.keys())}")
                                if show_meta_full and isinstance(show_meta_full, dict):
                                    # Set show's final IDs
                                    final_imdb_id = str(show_meta_full.get('imdb_id')).strip() if show_meta_full.get('imdb_id') and str(show_meta_full.get('imdb_id')).strip() else show_imdb_to_fetch_with
                                    # Correctly get the show's TMDB ID from 'ids.tmdb'
                                    final_tmdb_id = str(show_meta_full.get('ids', {}).get('tmdb')).strip() if show_meta_full.get('ids', {}).get('tmdb') else show_tmdb_known_from_search
                                    # REMOVED: The following line was overwriting final_tmdb_id, likely with a Trakt ID or an incorrect fallback.
                                    # final_tmdb_id = str(show_meta_full.get('id')).strip() if show_meta_full.get('id') and str(show_meta_full.get('id')).strip() else show_tmdb_known_from_search
                                    
                                    season_data_dict = show_meta_full.get('seasons', {})
                                    season_data = season_data_dict.get(str(s_num_int)) # API uses string keys for seasons
                                    if season_data is None: # Fallback for int key, though less likely
                                        season_data = season_data_dict.get(s_num_int)

                                    episode_data = None
                                    if season_data:
                                        episode_data_dict = season_data.get('episodes', {})
                                        episode_data = episode_data_dict.get(str(e_num_int)) # API uses string keys for episodes
                                        if episode_data is None: # Fallback for int key
                                            episode_data = episode_data_dict.get(e_num_int)
                                            
                                    if episode_data:
                                        episode_specific_tmdb_val = episode_data.get('id') 
                                        if not episode_specific_tmdb_val and isinstance(episode_data.get('ids'), dict):
                                            episode_specific_tmdb_val = episode_data.get('ids', {}).get('tmdb')
                                        episode_specific_tmdb_id_str = str(episode_specific_tmdb_val).strip() if episode_specific_tmdb_val and str(episode_specific_tmdb_val).strip() else None

                                        raw_first_aired = episode_data.get('first_aired')
                                        formatted_first_aired = None
                                        if raw_first_aired and isinstance(raw_first_aired, str):
                                            formatted_first_aired = raw_first_aired.split('T')[0]
                                        elif raw_first_aired:
                                            formatted_first_aired = str(raw_first_aired)

                                        metadata = {
                                            'title': show_meta_full.get('title'), 'year': show_meta_full.get('year'), 
                                            'imdb_id': final_imdb_id, 
                                            'tmdb_id': final_tmdb_id, # This will now correctly use the show's TMDB ID
                                            'season_number': s_num_int, 'episode_number': e_num_int,
                                            'episode_title': episode_data.get('title'), 
                                            'air_date': formatted_first_aired, # Use formatted date
                                            'release_date': formatted_first_aired, # Use formatted date
                                            'genres': show_meta_full.get('genres', [])
                                        }
                                    else: 
                                        logging.warning(f"[RcloneScan {task_id}] Episode S{s_num_int}E{e_num_int} not in show data for {final_imdb_id if final_imdb_id else show_imdb_to_fetch_with}")
                                else: 
                                    logging.warning(f"[RcloneScan {task_id}] get_show_metadata for {show_imdb_to_fetch_with} invalid. Using known show IDs.")
                                    final_imdb_id = show_imdb_to_fetch_with
                                    final_tmdb_id = show_tmdb_known_from_search
                            elif show_tmdb_known_from_search: 
                                logging.warning(f"[RcloneScan {task_id}] Only Show TMDB ID {show_tmdb_known_from_search} for '{parsed_title}'.")
                                final_tmdb_id = show_tmdb_known_from_search
                
                if not metadata: 
                    # If metadata is still None, but we have at least one ID (show or movie), log it.
                    # The previous ValueError for "Both IMDb and TMDB IDs missing" is only if *both* are missing *after* this whole block.
                    if final_imdb_id or final_tmdb_id:
                         logging.warning(f"[RcloneScan {task_id}] Full metadata object not constructed for '{parsed_title}', but found IDs: IMDb={final_imdb_id}, TMDB={final_tmdb_id} (File: {item_path.name})")
                    else: # This case should now be rarer with fallback ID assignments
                         raise ValueError(f"Metadata fetch failed AND no usable search/conversion ID ultimately found for '{parsed_title}' (File: {item_path.name})")

                # This final check remains important
                if not final_imdb_id and not final_tmdb_id: 
                    raise ValueError(f"Both IMDb and TMDB IDs are missing post-metadata processing for '{parsed_title}' (File: {item_path.name})")

            except Exception as e: 
                logging.warning(f"[RcloneScan {task_id}] Metadata processing stage for {item_path.name} failed: {e}", exc_info=True)
                metadata_errors += 1; update_progress(metadata_errors=metadata_errors); continue
            
            # 3. Prepare DB Item (original_file_path_str for original_path_for_symlink)
            current_time = datetime.now() # Changed from now_iso
            
            db_title = metadata.get('title') if metadata else parsed_title
            db_year = metadata.get('year') if metadata else parsed_year
            if current_parsed_type == 'episode' and metadata and metadata.get('title'):
                db_title = metadata.get('title') 
            elif not db_title: 
                 db_title = parsed_title

            if current_parsed_type == 'episode' and metadata and metadata.get('year'):
                db_year = metadata.get('year') 
            elif not db_year: 
                db_year = parsed_year

            # Determine release_date based on type and available keys
            final_release_date = None
            if metadata:
                if current_parsed_type == 'movie':
                    # For movies, prioritize 'release_date', then 'released'
                    raw_movie_release_date = metadata.get('release_date') or metadata.get('released')
                    if isinstance(raw_movie_release_date, str):
                        final_release_date = raw_movie_release_date.split('T')[0]
                    elif raw_movie_release_date: # If it exists but not string, log and set to None
                        logging.warning(f"[RcloneScan {task_id}] Movie release date key ('release_date' or 'released') was not a string: {raw_movie_release_date} for {item_path.name}")
                elif current_parsed_type == 'episode':
                    # For episodes, 'release_date' should already be formatted (from 'first_aired')
                    # 'air_date' can be a fallback if 'release_date' wasn't populated during episode metadata construction
                    raw_episode_release_date = metadata.get('release_date') or metadata.get('air_date')
                    if isinstance(raw_episode_release_date, str): # Should already be YYYY-MM-DD
                        final_release_date = raw_episode_release_date
                    elif raw_episode_release_date:
                         logging.warning(f"[RcloneScan {task_id}] Episode release date key ('release_date' or 'air_date') was not a string: {raw_episode_release_date} for {item_path.name}")

            item_content_source = 'external_webhook' if trigger_plex_update_on_success else 'scanned_item' # Determine content_source

            # Determine the value for filled_by_title
            filled_by_title_value = None
            if assumed_item_title_from_path:
                filled_by_title_value = assumed_item_title_from_path
                logging.debug(f"[RcloneScan {task_id}] Using assumed_item_title_from_path for filled_by_title: '{filled_by_title_value}' for item '{item_path.name}'")
            elif item_path and item_path.parent:
                filled_by_title_value = item_path.parent.name
                logging.debug(f"[RcloneScan {task_id}] Using parent folder name for filled_by_title: '{filled_by_title_value}' for item '{item_path.name}'")
            else:
                logging.warning(f"[RcloneScan {task_id}] Could not determine filled_by_title for item '{item_path.name if item_path else 'Unknown Item'}'. It will be None.")

            raw_genres_list = metadata.get('genres', []) if metadata else [] # Get genres as a list

            item_for_db = {
                'imdb_id': final_imdb_id, 
                'tmdb_id': final_tmdb_id,
                'title': db_title,
                'year': db_year,
                'release_date': final_release_date,
                'state': 'Collected', 
                'type': current_parsed_type,
                'season_number': metadata.get('season_number') if metadata else (s_num_int if current_parsed_type == 'episode' else None),
                'episode_number': metadata.get('episode_number') if metadata else (e_num_int if current_parsed_type == 'episode' else None),
                'episode_title': metadata.get('episode_title') if metadata else None, 
                'collected_at': current_time, # Use datetime object
                'original_collected_at': current_time, # Use datetime object
                'original_path_for_symlink': original_file_path_str,
                'version': current_parsed_version, 
                'filled_by_file': item_path.name,
                'filled_by_title': filled_by_title_value, # <<< USE THE DERIVED VALUE HERE
                'metadata_updated': current_time, # Use datetime object
                'genres': raw_genres_list, # Store raw list for get_symlink_path
                'content_source': item_content_source, # Use the determined content_source
            }
            item_for_db_filtered_for_symlink = {k: v for k, v in item_for_db.items() if v is not None} # Use this for get_symlink_path

            # 4. Generate Symlink Path
            try:
                # item_for_db_filtered_for_symlink has 'genres' as a list, which is correct for get_symlink_path
                symlink_dest_path = get_symlink_path(item_for_db_filtered_for_symlink, item_path.name, skip_jikan_lookup=True)
                if not symlink_dest_path: raise ValueError("get_symlink_path returned None")
                
                # Prepare item_for_db_filtered_for_db with genres as JSON string
                item_for_db_filtered_for_db = item_for_db_filtered_for_symlink.copy() # Start with a copy
                if 'genres' in item_for_db_filtered_for_db and isinstance(item_for_db_filtered_for_db['genres'], list):
                    item_for_db_filtered_for_db['genres'] = json.dumps(item_for_db_filtered_for_db['genres'])
                
                item_for_db_filtered_for_db['location_on_disk'] = symlink_dest_path # Add location_on_disk now

            except Exception as e:
                logging.warning(f"[RcloneScan {task_id}] Symlink path gen failed for {item_path.name}: {e}")
                symlink_errors += 1; update_progress(symlink_errors=symlink_errors); continue
            
            # 5. Dry Run or Execution
            if dry_run:
                preview_data = {
                    'original_file': original_file_path_str, 'parsed_title': parsed_title, 
                    'parsed_type': current_parsed_type, 'fetched_title': item_for_db_filtered_for_symlink.get('title'), # use _for_symlink version for title consistency in preview
                    'imdb_id': final_imdb_id, 'tmdb_id': final_tmdb_id,
                    'version': current_parsed_version, 'symlink_path': symlink_dest_path,
                    'action': 'CREATE DB Entry & Symlink'
                }
                preview_list.append(preview_data)
                update_progress(preview=preview_list, items_processed=items_processed)
            else:
                item_id_from_db = None
                try:
                    # add_media_item is called with item_for_db_filtered_for_db, 
                    # which has 'genres' as a JSON string
                    item_id_from_db = add_media_item(item_for_db_filtered_for_db)
                    if not item_id_from_db: raise Exception("add_media_item no ID returned")
                    items_added_to_db += 1; update_progress(items_added_to_db=items_added_to_db)
                except sqlite3.IntegrityError:
                    logging.warning(f"[RcloneScan {task_id}] DB IntegrityError for {item_path.name}, V:{current_parsed_version}. Likely duplicate.")
                    skipped_duplicates += 1; update_progress(skipped_duplicates=skipped_duplicates)
                    # If it's a duplicate, we should still mark original_file_path_str as processed if we intend to skip it next time
                    # However, if symlink creation was the goal, and DB entry exists, maybe try to symlink?
                    # For now, if DB entry is duplicate, then this file is not "successfully processed" into a *new* DB entry.
                    # This needs careful thought if we want to "adopt" existing DB entries.
                    # Current logic: if DB duplicate, then this file is not "successfully processed" into a *new* DB entry.
                    continue 
                except Exception as e:
                    logging.error(f"[RcloneScan {task_id}] DB add error for {item_path.name}: {e}", exc_info=True)
                    db_errors += 1; error_list.append(f"DB Add Error ({item_path.name}): {e}"); update_progress(db_errors=db_errors, errors=error_list)
                    continue

                if item_id_from_db:
                    try:
                        # Determine if symlink verification should be skipped based on the context
                        # Webhook (trigger_plex_update_on_success=True) -> skip_verification=False (i.e., DO verify)
                        # Debug tool (trigger_plex_update_on_success=False) -> skip_verification=True (i.e., DO NOT verify)
                        skip_verification_for_symlink = not trigger_plex_update_on_success
                        
                        symlink_success = create_symlink(
                            original_file_path_str, 
                            symlink_dest_path, 
                            media_item_id=item_id_from_db, 
                            skip_verification=skip_verification_for_symlink # Dynamically set based on context
                        )
                        if not symlink_success: raise Exception("create_symlink returned False")
                        symlinks_created += 1; update_progress(symlinks_created=symlinks_created)
                        
                        # Successfully processed, add to persistent progress and save
                        processed_original_files.add(original_file_path_str)
                        save_rclone_progress()
                        
                        # --- Conditionally Add Plex Update Call ---
                        if trigger_plex_update_on_success: # Check the new parameter
                            plex_url = get_setting('File Management', 'plex_url_for_symlink', '')
                            plex_token = get_setting('File Management', 'plex_token_for_symlink', '')
                            
                            if plex_url and plex_token:
                                logging.info(f"[RcloneScan {task_id}] Plex configured and update triggered. Attempting library update for: {symlink_dest_path}")
                                try:
                                    # Make sure item_for_db_filtered_for_db has the necessary info (title, year, type etc.)
                                    plex_update_item(item=item_for_db_filtered_for_db)
                                    logging.info(f"[RcloneScan {task_id}] Plex library update triggered for: {symlink_dest_path}")
                                except Exception as plex_err:
                                    logging.error(f"[RcloneScan {task_id}] Failed to trigger Plex update for {symlink_dest_path}: {plex_err}", exc_info=True)
                            else:
                                 logging.debug(f"[RcloneScan {task_id}] Plex URL/Token not configured in 'File Management' settings. Skipping Plex update despite trigger.")
                        else:
                            logging.debug(f"[RcloneScan {task_id}] Plex update not triggered for this task run.")
                        # --- End Plex Update Call ---
                        
                        # --- Send Notification if from external_webhook ---
                        if item_for_db_filtered_for_db.get('content_source') == 'external_webhook':
                            try:
                                notification_item = {
                                    'type': item_for_db_filtered_for_db.get('type'), # 'movie' or 'episode'
                                    'title': item_for_db_filtered_for_db.get('title'),
                                    'year': item_for_db_filtered_for_db.get('year'),
                                    'tmdb_id': str(item_for_db_filtered_for_db.get('tmdb_id')) if item_for_db_filtered_for_db.get('tmdb_id') else None,
                                    'imdb_id': item_for_db_filtered_for_db.get('imdb_id'),
                                    'original_collected_at': item_for_db_filtered_for_db.get('collected_at').isoformat() if item_for_db_filtered_for_db.get('collected_at') else datetime.now().isoformat(),
                                    'version': item_for_db_filtered_for_db.get('version'),
                                    'is_upgrade': False, # New items from rclone webhook are not considered upgrades here
                                    'media_type': 'tv' if item_for_db_filtered_for_db.get('type') == 'episode' else 'movie',
                                    'new_state': 'Collected',
                                    'content_source': item_for_db_filtered_for_db.get('content_source'), # Should be 'external_webhook'
                                    'filled_by_file': item_for_db_filtered_for_db.get('filled_by_file') # ADDED this line
                                    # 'content_source_detail': os.path.basename(original_file_path_str) # REMOVED this line
                                }
                                if item_for_db_filtered_for_db.get('type') == 'episode':
                                    notification_item.update({
                                        'season_number': item_for_db_filtered_for_db.get('season_number'),
                                        'episode_number': item_for_db_filtered_for_db.get('episode_number'),
                                        'episode_title': item_for_db_filtered_for_db.get('episode_title')
                                    })
                                
                                logging.info(f"[RcloneScan {task_id}] Sending 'collected' notification for item: {notification_item.get('title')}")
                                send_notifications([notification_item], get_enabled_notifications(), notification_category='collected')
                            except Exception as notify_err:
                                logging.error(f"[RcloneScan {task_id}] Failed to send notification for {item_for_db_filtered_for_db.get('title')}: {notify_err}", exc_info=True)
                        # --- End Notification ---
                        
                    except Exception as e:
                        logging.error(f"[RcloneScan {task_id}] Symlink creation error for {symlink_dest_path} (DB ID {item_id_from_db}): {e}", exc_info=True)
                        symlink_errors += 1; error_list.append(f"Symlink Error ({item_path.name}): {e}"); update_progress(symlink_errors=symlink_errors, errors=error_list)
            update_progress(items_processed=items_processed)


        final_message_parts = [
            f"Rclone scan finished. Scanned: {total_files_scanned}, Media Files Initially Found: {media_files_found}, Items Chosen for Processing: {items_processed}."
        ]
        if dry_run:
            final_message_parts.append(f"Dry Run Preview: {len(preview_list)} items.")
        else:
             final_message_parts.append(f"DB Added: {items_added_to_db}, Symlinks Created: {symlinks_created}.")
        if skipped_previously_processed_count > 0: final_message_parts.append(f"Skipped (Previously Processed): {skipped_previously_processed_count}.")
        if skipped_due_to_size_count > 0: final_message_parts.append(f"Skipped (Movie Size Below Threshold): {skipped_due_to_size_count}.") # Updated counter
        if skipped_duplicates > 0: final_message_parts.append(f"Skipped (DB Duplicates): {skipped_duplicates}.")
        # ... (other error counts) ...
        error_counts_str = []
        if parser_errors > 0: error_counts_str.append(f"Parser: {parser_errors}")
        if metadata_errors > 0: error_counts_str.append(f"Metadata: {metadata_errors}")
        if db_errors > 0: error_counts_str.append(f"DB: {db_errors}")
        if symlink_errors > 0: error_counts_str.append(f"Symlink: {symlink_errors}")
        if error_counts_str: final_message_parts.append(f"Errors ({', '.join(error_counts_str)}).")

        final_message = " ".join(final_message_parts)
        success_status = (parser_errors == 0 and metadata_errors == 0 and db_errors == 0 and symlink_errors == 0)

        update_progress(
            status='complete', message=final_message, complete=True, success=success_status,
            total_files_scanned=total_files_scanned, media_files_found=media_files_found, 
            items_processed=items_processed, items_added_to_db=items_added_to_db,
            symlinks_created=symlinks_created, parser_errors=parser_errors,
            metadata_errors=metadata_errors, db_errors=db_errors, symlink_errors=symlink_errors,
            skipped_duplicates=skipped_duplicates, 
            skipped_due_to_size=skipped_due_to_size_count, # Updated counter
            skipped_previously_processed=skipped_previously_processed_count
        )

    except Exception as e:
        logging.error(f"[RcloneScan {task_id}] Critical error in Rclone scan task: {e}", exc_info=True)
        update_progress(status='error', message=f'Task failed: {e}', complete=True, success=False)
    finally:
        try:
             if symlink_base_path_setting_backup is not None:
                 set_setting('File Management', 'symlinked_files_path', symlink_base_path_setting_backup)
        except Exception as restore_err:
             logging.error(f"[RcloneScan {task_id}] Failed to restore symlink path setting: {restore_err}")
             # ... (append to errors in progress dict) ...
        # Save final progress one last time, e.g. if loop broke early
        if not dry_run: save_rclone_progress() 
        threading.Timer(300, lambda: rclone_scan_progress.pop(task_id, None)).start()

@debug_bp.route('/api/rclone_to_symlinks', methods=['POST'])
@admin_required
def rclone_to_symlinks_route():
    """API endpoint to initiate the Rclone mount scan and symlink creation."""
    rclone_mount_path = request.form.get('rclone_mount_path')
    symlink_base_path = request.form.get('symlink_base_path')
    dry_run = request.form.get('dry_run') == 'on' # Checkbox value is 'on' if checked
    # For manual trigger from debug page, assumed_item_title_from_path will be None
    # as it's scanning a whole directory, not a specific item signaled by webhook.
    assumed_item_title_from_path_manual = None 


    if not rclone_mount_path:
        return jsonify({'success': False, 'error': 'Rclone Mount Path is required.'}), 400
    if not symlink_base_path:
         return jsonify({'success': False, 'error': 'Symlink Base Path is required.'}), 400

    import uuid
    task_id = str(uuid.uuid4())

    # Start the background task
    thread = threading.Thread(
        target=_run_rclone_to_symlink_task,
        args=(rclone_mount_path, symlink_base_path, dry_run, task_id, False, assumed_item_title_from_path_manual) # Pass False for trigger_plex_update and None for assumed_item_title
    )
    thread.daemon = True
    thread.start()

    return jsonify({'success': True, 'task_id': task_id}), 202


@debug_bp.route('/api/rclone_scan_progress/<task_id>')
@admin_required # Add protection here as well
def rclone_scan_progress_stream(task_id):
    """SSE endpoint for tracking Rclone scan progress."""
    def generate():
        while True:
            if task_id not in rclone_scan_progress:
                progress = {'status': 'error', 'message': 'Task not found or expired', 'complete': True}
                yield f"data: {json.dumps(progress)}\n\n"
                break

            progress = rclone_scan_progress[task_id]
            yield f"data: {json.dumps(progress)}\n\n"

            if progress.get('complete', False):
                break

            time.sleep(1) # Poll interval

    return Response(stream_with_context(generate()), mimetype='text/event-stream')

# --- End Rclone Mount to Symlinks Logic ---

# --- Riven Symlink Recovery Routes ---

@debug_bp.route('/recover_riven_symlinks')
@admin_required
def recover_riven_symlinks_page():
    """Renders the Riven symlink recovery page."""
    # For now, it can render the same template. A new template recover_riven_symlinks.html might be needed later.
    return render_template('recover_symlinks.html', recovery_type='riven') # Pass type for potential JS differentiation

def parse_riven_symlink(symlink_path: Path):
    """Parses a Riven symlink path based on filename patterns, not templates."""
    filename = symlink_path.name
    parsed_data = {
        'symlink_path': str(symlink_path),
        'original_path_for_symlink': None, # Populated in analyze_riven_symlinks
        'media_type': None, # Determined below
        'imdb_id': None, # Determined below
        'tmdb_id': None, # Populated by get_metadata
        'title': None, # Populated by get_metadata
        'year': None, # Populated by get_metadata
        'season_number': None, # Determined below
        'episode_number': None, # Determined below
        'episode_title': None, # Populated by get_metadata
        'version': None, # Populated by reverse_parser in analyze_riven_symlinks
        'original_filename': None, # Populated in analyze_riven_symlinks
        'is_anime': False # Populated by get_metadata
    }

    # Robust S/E matching from filename
    se_filename_match = re.search(r'[Ss](\d{1,2})[EeXx](\d{1,3})|Season\s?(\d{1,2})\s?Episode\s?(\d{1,3})|(\d{1,2})[Xx](\d{1,3})', filename)

    parent_dir_name = symlink_path.parent.name if symlink_path.parent else ""
    season_from_parent_match = re.search(r'[Ss](?:eason)?\s?(\d+)', parent_dir_name)

    # Determine if it's an episode
    if se_filename_match:
        parsed_data['media_type'] = 'episode'
        # Extract S/E from filename groups
        if se_filename_match.group(1) is not None and se_filename_match.group(2) is not None: # SxxExx
            parsed_data['season_number'] = int(se_filename_match.group(1))
            parsed_data['episode_number'] = int(se_filename_match.group(2))
        elif se_filename_match.group(3) is not None and se_filename_match.group(4) is not None: # Season xx Episode xx
            parsed_data['season_number'] = int(se_filename_match.group(3))
            parsed_data['episode_number'] = int(se_filename_match.group(4))
        elif se_filename_match.group(5) is not None and se_filename_match.group(6) is not None: # xxXx
            parsed_data['season_number'] = int(se_filename_match.group(5))
            parsed_data['episode_number'] = int(se_filename_match.group(6))
    elif season_from_parent_match: # Season in parent folder, check filename for simple episode number
        parsed_data['media_type'] = 'episode'
        parsed_data['season_number'] = int(season_from_parent_match.group(1))
        # Try to get simple episode number from filename, e.g., "01.mkv", "E01.mkv"
        ep_num_match = re.search(r'(?:[Ee](?:pisode)?)?\s?(\d+)\.[^.]+$', filename) # Matches "01.mkv", "E01.mkv", "episode 01.mkv"
        if ep_num_match:
            ep_val = int(ep_num_match.group(1))
            if 1 <= ep_val <= 200: # Sanity check
                parsed_data['episode_number'] = ep_val
    else:
        parsed_data['media_type'] = 'movie'

    # IMDb ID Extraction
    if parsed_data['media_type'] == 'episode':
        if not (symlink_path.parent and symlink_path.parent.parent):
            logging.warning(f"RIVEN (EPISODE): Path '{symlink_path}' too short for IMDb ID from grandfather directory.")
            return None
        grandfather_dir_name = symlink_path.parent.parent.name
        imdb_match = re.search(r'(tt\d{7,})', grandfather_dir_name, re.IGNORECASE)
        if imdb_match:
            parsed_data['imdb_id'] = imdb_match.group(1)
        else:
            logging.warning(f"RIVEN (EPISODE): IMDb ID not found in grandfather directory '{grandfather_dir_name}' for episode file '{symlink_path}'.")
            return None
        
        # Final check for S/E numbers for episodes
        if parsed_data.get('season_number') is None or parsed_data.get('episode_number') is None:
            logging.warning(f"RIVEN (EPISODE): Incomplete S/E numbers for '{symlink_path}'. S={parsed_data.get('season_number')}, E={parsed_data.get('episode_number')}. Filename: '{filename}', Parent: '{parent_dir_name}'.")
            return None

    elif parsed_data['media_type'] == 'movie':
        # Try filename first for movie IMDb
        imdb_match_file = re.search(r'(tt\d{7,})', filename, re.IGNORECASE)
        if imdb_match_file:
            parsed_data['imdb_id'] = imdb_match_file.group(1)
        # If not in filename, try immediate parent directory for movie IMDb
        elif symlink_path.parent:
            imdb_match_parent = re.search(r'(tt\d{7,})', parent_dir_name, re.IGNORECASE)
            if imdb_match_parent:
                parsed_data['imdb_id'] = imdb_match_parent.group(1)
            else:
                logging.warning(f"RIVEN (MOVIE): IMDb ID not found in filename '{filename}' or parent directory '{parent_dir_name}' for movie file '{symlink_path}'.")
                return None
        else: # No parent, and not in filename
            logging.warning(f"RIVEN (MOVIE): IMDb ID not found in filename '{filename}' and no parent directory for movie file '{symlink_path}'.")
            return None
    else: # Should not happen if media_type is always set
        logging.error(f"RIVEN: media_type not determined for {symlink_path}")
        return None

    # Final check for any IMDb ID
    if not parsed_data.get('imdb_id'):
        logging.warning(f"RIVEN: IMDb ID could not be resolved for path '{symlink_path}'.")
        return None

    logging.debug(f"RIVEN: Parsed initial data from {filename}: IMDb={parsed_data['imdb_id']}, Type={parsed_data['media_type']}, S={parsed_data.get('season_number')}, E={parsed_data.get('episode_number')}")
    return parsed_data

def _run_riven_analysis_thread(symlink_root_path_str, original_root_path_str, task_id):
    """The actual Riven analysis logic, run in a background thread."""
    global riven_analysis_progress

    db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    temp_recovery_dir = os.path.join(db_content_dir, 'tmp_riven_recovery')
    try:
        os.makedirs(temp_recovery_dir, exist_ok=True)
    except OSError as e:
        logging.error(f"RIVEN: Failed to create temporary recovery directory {temp_recovery_dir}: {e}")
        pass 
    
    recovery_file_path = os.path.join(temp_recovery_dir, f"riven_recovery_{task_id}.jsonl")

    riven_analysis_progress[task_id] = {
        'status': 'starting',
        'message': 'Initializing Riven analysis...',
        'total_items_scanned': 0,
        'total_symlinks_processed': 0,
        'total_files_processed': 0,
        'items_found': 0,
        'parser_errors': 0,
        'metadata_errors': 0,
        'recoverable_items_preview': [],
        'recovery_file_path': None,
        'complete': False
    }

    def update_progress(**kwargs):
        if task_id in riven_analysis_progress:
            riven_analysis_progress[task_id].update(kwargs)
            preview = riven_analysis_progress[task_id]['recoverable_items_preview']
            if len(preview) > 5:
                 riven_analysis_progress[task_id]['recoverable_items_preview'] = preview[:5]
        else:
            logging.warning(f"RIVEN: Task ID {task_id} not found in progress dict during update.")

    recovery_file = None
    try:
        recovery_file = open(recovery_file_path, 'a', encoding='utf-8')

        symlink_root_path = Path(symlink_root_path_str)
        original_root_path = Path(original_root_path_str) if original_root_path_str else None

        if not symlink_root_path.is_dir():
            raise ValueError('RIVEN: Symlink Root Path must be a valid directory.')
        if original_root_path and not original_root_path.is_dir():
            raise ValueError('RIVEN: Original Root Path must be valid if provided.')

        symlink_organize_by_resolution = get_setting('File Management', 'symlink_organize_by_resolution', False)
        ignored_extensions = {'.srt', '.sub', '.idx', '.nfo', '.txt', '.jpg', '.png', '.db', '.partial', '.!qB'}
        riven_type_folders = ["anime_movies", "anime_shows", "movies", "shows"]
        
        # Define actual resolution subfolder names to check if organization is on
        actual_resolution_subfolder_names = ["2160p", "1080p"] 

        total_items_scanned = 0
        total_symlinks_processed = 0
        total_files_processed = 0
        items_found = 0
        parser_errors = 0
        metadata_errors = 0
        recoverable_items_preview = []

        # --- Nested helper function to process items in a directory ---
        def _scan_riven_directory_recursive(path_to_scan: Path, scan_description: str):
            nonlocal total_items_scanned, total_symlinks_processed, total_files_processed, items_found, parser_errors, metadata_errors, recoverable_items_preview
            
            update_progress(message=f'RIVEN: Scanning {scan_description}...')
            try:
                for item_path in path_to_scan.rglob('*'):
                    total_items_scanned += 1
                    if total_items_scanned % 100 == 0:
                        update_progress(
                            total_items_scanned=total_items_scanned,
                            total_symlinks_processed=total_symlinks_processed,
                            total_files_processed=total_files_processed,
                            items_found=items_found,
                            parser_errors=parser_errors,
                            metadata_errors=metadata_errors,
                            message=f'RIVEN: Scanned {total_items_scanned} items...'
                         )

                    if item_path.suffix.lower() in ignored_extensions:
                        continue

                    if item_path.is_file() or item_path.is_symlink():
                        if item_path.is_symlink():
                            total_symlinks_processed += 1
                        else:
                            total_files_processed += 1

                        parsed_data = parse_riven_symlink(item_path)
                        if not parsed_data:
                            parser_errors += 1
                            continue

                        original_path_obj = None
                        if item_path.is_symlink():
                            try:
                                target_path_str = os.readlink(str(item_path))
                                if not os.path.isabs(target_path_str):
                                    target_path_str = os.path.abspath(os.path.join(item_path.parent, target_path_str))
                                original_path_obj = Path(target_path_str)
                            except Exception as e:
                                logging.error(f"RIVEN: Error reading symlink target for {item_path}: {e}")
                                parsed_data['original_path_for_symlink'] = f"Error: Cannot read link target ({e})"
                                parsed_data['original_filename'] = item_path.name
                        elif item_path.is_file():
                            original_path_obj = item_path

                        if original_path_obj and original_path_obj.is_file():
                            parsed_data['original_path_for_symlink'] = str(original_path_obj)
                            parsed_data['original_filename'] = original_path_obj.name
                        elif 'original_path_for_symlink' not in parsed_data :
                            if original_path_obj:
                                 logging.warning(f"RIVEN: Symlink target {original_path_obj} is not a file for {item_path}.")
                                 parsed_data['original_path_for_symlink'] = f"Error: Target not a file ({original_path_obj})"
                            else:
                                logging.warning(f"RIVEN: Could not determine original file for {item_path}.")
                                parsed_data['original_path_for_symlink'] = "Error: Original path unknown"
                            parsed_data['original_filename'] = item_path.name
                        
                        if not parsed_data.get('original_filename') or "Error:" in str(parsed_data.get('original_path_for_symlink')):
                            parser_errors += 1
                            logging.warning(f"RIVEN: Skipping item {item_path.name} due to missing original file information.")
                            continue
                        
                        filename_for_version = parsed_data.get('original_filename')
                        if filename_for_version:
                            try:
                                version_raw = parse_filename_for_version(filename_for_version)
                                parsed_data['version'] = version_raw.strip('*') if version_raw else 'Default'
                            except Exception as e:
                                parsed_data['version'] = 'Default'
                        else:
                            parsed_data['version'] = 'Default'
                            
                        if parsed_data['imdb_id']:
                            metadata_args = {
                                'imdb_id': parsed_data['imdb_id'],
                                'item_media_type': parsed_data.get('media_type')
                                # season_number and episode_number are not directly used by get_metadata for initial fetch
                            }
                            try:
                                metadata_args['original_item'] = parsed_data 
                                from metadata.metadata import get_metadata
                                # This metadata is likely show-level for episodes
                                metadata = get_metadata(**metadata_args) 

                                if metadata:
                                    # Populate base parsed_data from show-level metadata
                                    parsed_data['title'] = metadata.get('title', parsed_data.get('title')) # Prefer metadata title
                                    parsed_data['year'] = metadata.get('year', parsed_data.get('year'))
                                    # Corrected TMDB ID extraction
                                    parsed_data['tmdb_id'] = str(metadata.get('ids', {}).get('tmdb')).strip() if metadata.get('ids', {}).get('tmdb') else parsed_data.get('tmdb_id')
                                    # Use show's release_date as a fallback if episode-specific one isn't found
                                    parsed_data['release_date'] = metadata.get('release_date') 
                                    genres = metadata.get('genres', []) # Use genres from show metadata
                                    if isinstance(genres, str): # Ensure genres is a list
                                        try: genres = json.loads(genres)
                                        except json.JSONDecodeError: genres = [g.strip() for g in genres.split(',') if g.strip()]
                                    if not isinstance(genres, list): genres = [str(genres)]
                                    parsed_data['is_anime'] = any('anime' in genre.lower() for genre in genres)

                                    # If it's an episode, try to get specific episode title and air date
                                    if parsed_data['media_type'] == 'episode' and parsed_data.get('season_number') is not None and parsed_data.get('episode_number') is not None:
                                        try:
                                            # Use integer keys for lookup
                                            s_num_int = int(parsed_data['season_number'])
                                            e_num_int = int(parsed_data['episode_number'])
                                            
                                            # Fetch full show details to navigate to episode
                                            direct_api = DirectAPI() # Initialize DirectAPI
                                            # The imdb_id in parsed_data should be the show's IMDb ID
                                            full_show_details, _ = direct_api.get_show_metadata(imdb_id=parsed_data['imdb_id']) 
                                            
                                            if full_show_details:
                                                # Access seasons and episodes using integer keys
                                                season_data = full_show_details.get('seasons', {}).get(s_num_int)
                                                if season_data:
                                                    episode_data = season_data.get('episodes', {}).get(e_num_int)
                                                    if episode_data:
                                                        parsed_data['episode_title'] = episode_data.get('title')
                                                        # Prefer episode's air_date if available
                                                        episode_air_date = episode_data.get('first_aired')
                                                        if episode_air_date:
                                                            parsed_data['release_date'] = episode_air_date
                                                        logging.debug(f"RIVEN: Fetched episode title '{parsed_data['episode_title']}' and air_date '{parsed_data['release_date']}' for S{s_num_int}E{e_num_int}")
                                                    else:
                                                        logging.warning(f"RIVEN: Episode S{s_num_int}E{e_num_int} not found in details for {parsed_data['imdb_id']}.")
                                                else:
                                                    logging.warning(f"RIVEN: Season {s_num_int} not found in details for {parsed_data['imdb_id']}.")
                                            else:
                                                logging.warning(f"RIVEN: Could not fetch full show details via DirectAPI for {parsed_data['imdb_id']} to get episode title.")
                                        except ValueError: # Handles case where season_number or episode_number can't be int
                                            logging.error(f"RIVEN: Invalid non-integer season/episode number for {parsed_data['imdb_id']}: S='{parsed_data.get('season_number')}', E='{parsed_data.get('episode_number')}'. Cannot fetch episode details.")
                                        except Exception as ep_fetch_exc:
                                            logging.error(f"RIVEN: Error fetching specific episode details for {parsed_data['imdb_id']} S{parsed_data.get('season_number')}E{parsed_data.get('episode_number')}: {ep_fetch_exc}", exc_info=False)
                                    
                                    # Ensure 'genres' key exists in parsed_data for prospective_db_item
                                    parsed_data['genres'] = genres # Store the list of genres

                                    # --- Calculate new symlink path ---
                                    current_original_filename_with_ext = parsed_data.get('original_filename')
                                    prospective_db_item = {
                                        'title': parsed_data.get('title'), 
                                        'year': parsed_data.get('year'),
                                        'type': parsed_data.get('media_type'), 
                                        'imdb_id': parsed_data.get('imdb_id'),
                                        'tmdb_id': parsed_data.get('tmdb_id'), 
                                        'season_number': parsed_data.get('season_number'),
                                        'episode_number': parsed_data.get('episode_number'), 
                                        'version': parsed_data.get('version', 'Default'),
                                        'is_anime': parsed_data.get('is_anime', False), 
                                        'episode_title': parsed_data.get('episode_title'), # Now this should be populated
                                        'release_date': parsed_data.get('release_date'), # And this might be more specific
                                        'filled_by_file': current_original_filename_with_ext,
                                        'genres': parsed_data.get('genres') # Pass genres to get_symlink_path
                                    }
                                    prospective_db_item_filtered = {k: v for k, v in prospective_db_item.items() if v is not None}

                                    try:
                                        new_symlink_location = get_symlink_path(
                                            prospective_db_item_filtered, # This now contains 'filled_by_file'
                                            parsed_data.get('original_filename'), # This is the second argument 'original_file'
                                            skip_jikan_lookup=True
                                        )
                                        if not new_symlink_location:
                                            raise ValueError("get_symlink_path returned None or empty.")
                                        parsed_data['newly_calculated_symlink_path'] = new_symlink_location
                                    except Exception as e_sym_path:
                                        logging.error(f"RIVEN: Error calculating new symlink path for {parsed_data.get('title')}: {e_sym_path}")
                                        metadata_errors += 1
                                        continue

                                    items_found += 1
                                    try:
                                        recovery_file.write(json.dumps(parsed_data) + '\n')
                                    except Exception as write_err:
                                        logging.error(f"RIVEN: Error writing item to recovery file {recovery_file_path}: {write_err}")
                                    
                                    if len(recoverable_items_preview) < 5:
                                         recoverable_items_preview.append(parsed_data)
                                    update_progress(items_found=items_found, recoverable_items_preview=recoverable_items_preview)
                                else: # metadata fetch failed
                                    metadata_errors += 1
                                    logging.warning(f"RIVEN: Metadata fetch failed for IMDb {parsed_data['imdb_id']} ({item_path.name}).")
                            except Exception as e: # Error during metadata processing block
                                logging.error(f"RIVEN: Metadata processing error for {parsed_data.get('imdb_id', 'Unknown IMDb')} ({item_path.name}): {e}", exc_info=False)
                                metadata_errors += 1
                        else: # No IMDb ID was parsed
                            parser_errors += 1 # This case should be caught by parse_riven_symlink returning None now
                            logging.warning(f"RIVEN: Skipping {item_path.name} as no IMDb ID was parsed (should have been caught earlier).")

            except Exception as e_rglob:
                logging.error(f"RIVEN: Error during rglob scan of {path_to_scan}: {e_rglob}", exc_info=True)
        # --- End of nested helper function ---

        update_progress(status='scanning', message='Starting Riven directory scan...')

        # Iterate through the Riven-specific type folders
        for type_folder_name in riven_type_folders:
            # Path 1: Scan directly within the type folder (e.g., /mnt/zurg-symlinked/movies)
            base_type_path = symlink_root_path / type_folder_name
            if base_type_path.is_dir():
                _scan_riven_directory_recursive(base_type_path, type_folder_name)
            else:
                logging.warning(f"RIVEN: Base directory not found or not accessible: {base_type_path}")

            # Path 2: If resolution organization is enabled, scan within resolution subfolders 
            # (e.g., /mnt/zurg-symlinked/movies/2160p)
            if symlink_organize_by_resolution:
                for res_subfolder_name in actual_resolution_subfolder_names:
                    resolution_specific_path = base_type_path / res_subfolder_name
                    scan_target_description = f'{type_folder_name}/{res_subfolder_name}'
                    if resolution_specific_path.is_dir():
                        _scan_riven_directory_recursive(resolution_specific_path, scan_target_description)
                    else:
                        # This is not necessarily an error, could just be that this resolution isn't used for this type
                        logging.debug(f"RIVEN: Optional resolution directory not found, skipping: {resolution_specific_path}")
                    
        update_progress(
            status='complete',
            message='Riven analysis finished.',
            complete=True,
            recovery_file_path=recovery_file_path if items_found > 0 else None,
            total_items_scanned=total_items_scanned,
            total_symlinks_processed=total_symlinks_processed,
            total_files_processed=total_files_processed,
            items_found=items_found,
            parser_errors=parser_errors,
            metadata_errors=metadata_errors
        )

    except Exception as e:
        logging.error(f"RIVEN: Analysis thread error for task {task_id}: {e}", exc_info=True)
        update_progress(status='error', message=f'RIVEN: Analysis failed: {e}', complete=True)
    finally:
        if recovery_file:
            try:
                recovery_file.close()
            except Exception as close_err:
                 logging.error(f"RIVEN: Error closing recovery file {recovery_file_path}: {close_err}")
        pass

@debug_bp.route('/analyze_riven_symlinks', methods=['POST'])
@admin_required
def analyze_riven_symlinks():
    """Initiates the Riven symlink analysis in a background thread and returns a task ID."""
    import uuid
    symlink_root_path_str = request.form.get('symlink_root_path')
    original_root_path_str = request.form.get('original_root_path')

    if not symlink_root_path_str:
        return jsonify({'success': False, 'error': 'RIVEN: Symlink Root Path is required.'}), 400

    task_id = str(uuid.uuid4())
    
    thread = threading.Thread(
        target=_run_riven_analysis_thread, # Call new analysis thread
        args=(symlink_root_path_str, original_root_path_str, task_id)
    )
    thread.daemon = True
    thread.start()

    return jsonify({'success': True, 'task_id': task_id})

@debug_bp.route('/riven_analysis_progress/<task_id>') # New route
@admin_required
def riven_analysis_progress_stream(task_id): # New function
    """SSE endpoint for tracking Riven analysis progress."""
    def generate():
        while True:
            if task_id not in riven_analysis_progress: # Use new global
                progress = {'status': 'error', 'message': 'RIVEN: Task not found or expired', 'complete': True}
                yield f"data: {json.dumps(progress)}\n\n"
                break
                
            progress = riven_analysis_progress[task_id] # Use new global
            yield f"data: {json.dumps(progress)}\n\n"
            
            if progress.get('complete', False):
                break
                
            time.sleep(1)
            
    return Response(stream_with_context(generate()), mimetype='text/event-stream')

@debug_bp.route('/perform_riven_recovery', methods=['POST']) # New route
@admin_required
def perform_riven_recovery(): # New function
    """Recovers all items found during a specific Riven analysis task by reading from its recovery file."""
    from database import add_media_item
    # Ensure create_symlink is available
    from utilities.local_library_scan import create_symlink

    data = request.get_json()
    task_id = data.get('task_id')

    if not task_id:
        return jsonify({'success': False, 'error': 'RIVEN: Missing task_id.'}), 400

    if task_id not in riven_analysis_progress or not riven_analysis_progress[task_id].get('complete'): # Use new global
        return jsonify({'success': False, 'error': f'RIVEN: Analysis task {task_id} not found or not complete.'}), 404

    analysis_result = riven_analysis_progress[task_id] # Use new global
    recovery_file_path = analysis_result.get('recovery_file_path')
    expected_items = analysis_result.get('items_found', 0)

    if not recovery_file_path:
        if expected_items == 0:
             return jsonify({'success': True, 'message': 'RIVEN: Analysis found no items to recover.', 'successful_recoveries': 0, 'failed_recoveries': 0}), 200
        else:
            return jsonify({'success': False, 'error': f'RIVEN: Recovery file path not found for completed task {task_id}. Analysis might have failed partially?'}), 404

    if not os.path.exists(recovery_file_path):
         return jsonify({'success': False, 'error': f'RIVEN: Recovery file not found at {recovery_file_path}. It might have been deleted or analysis failed.'}), 404

    recovery_file = None
    successful_recoveries = 0
    failed_recoveries = 0
    errors = []

    try:
        recovery_file = open(recovery_file_path, 'r', encoding='utf-8')
        logging.info(f"RIVEN: Starting recovery from file: {recovery_file_path} for task {task_id}")

        for line_num, line in enumerate(recovery_file):
            item_data = None
            try:
                line = line.strip()
                if not line: continue

                item_data = json.loads(line)
                now_iso = datetime.now()

                # Paths for DB entry and symlink creation
                original_source_file = item_data.get('original_path_for_symlink')
                newly_calculated_symlink_dest = item_data.get('newly_calculated_symlink_path')

                if not original_source_file or not newly_calculated_symlink_dest:
                    failed_recoveries += 1
                    error_msg = f"RIVEN: Skipped recovery for item on line {line_num + 1} due to missing original_path or newly_calculated_symlink_path."
                    errors.append(error_msg)
                    logging.warning(error_msg)
                    continue

                db_item_for_insert = {
                    'imdb_id': item_data.get('imdb_id'),
                    'tmdb_id': item_data.get('tmdb_id'),
                    'title': item_data.get('title'),
                    'year': item_data.get('year'),
                    'release_date': item_data.get('release_date'),
                    'state': 'Collected',
                    'type': item_data.get('media_type'),
                    'season_number': item_data.get('season_number'),
                    'episode_number': item_data.get('episode_number'),
                    'episode_title': item_data.get('episode_title'),
                    'collected_at': now_iso,
                    'original_collected_at': now_iso,
                    'original_path_for_symlink': original_source_file, # Store the true original
                    'version': item_data.get('version', 'Default'),
                    'filled_by_file': item_data.get('original_filename'), # Original filename
                    'metadata_updated': now_iso,
                    'wake_count': 0,
                    'location_on_disk': newly_calculated_symlink_dest # The new symlink path
                }
                db_item_filtered = {k: v for k, v in db_item_for_insert.items() if v is not None}

                if not db_item_filtered.get('imdb_id') or not db_item_filtered.get('type'):
                    raise ValueError(f"Missing essential data (imdb_id or type) after filtering")

                try:
                    item_id = add_media_item(db_item_filtered)
                    if item_id:
                        # --- Create the new symlink ---
                        try:
                            symlink_created_successfully = create_symlink(
                                original_source_file, 
                                newly_calculated_symlink_dest, 
                                media_item_id=item_id, # Pass media_item_id for verification queue
                                skip_verification=True # Or False if you want immediate verification
                            )
                            if symlink_created_successfully:
                                successful_recoveries += 1
                                logging.info(f"RIVEN: Successfully created DB entry (ID: {item_id}) and symlink for: {newly_calculated_symlink_dest}")
                            else:
                                # Symlink creation failed, this is a partial failure for this item
                                failed_recoveries += 1
                                error_msg = f"RIVEN: DB entry created (ID: {item_id}) but FAILED to create symlink from '{original_source_file}' to '{newly_calculated_symlink_dest}'."
                                errors.append(error_msg)
                                logging.error(error_msg)
                                # Consider if the DB entry should be rolled back or marked differently.
                                # For now, it's a failed recovery.
                        except Exception as e_sym_create:
                            failed_recoveries += 1
                            error_msg = f"RIVEN: DB entry created (ID: {item_id}) but EXCEPTION during symlink creation for '{newly_calculated_symlink_dest}': {e_sym_create}"
                            errors.append(error_msg)
                            logging.error(error_msg, exc_info=True)
                        # --- End symlink creation ---
                    else:
                        # add_media_item returning None or False (not an ID)
                        failed_recoveries += 1 # Count as a failed recovery
                        item_desc = f"item on line {line_num + 1} (Path: {item_data.get('scanned_path', 'Unknown')}, Original: {original_source_file})"
                        error_msg = f"RIVEN: Failed to add DB entry for {item_desc} (add_media_item returned no ID)."
                        errors.append(error_msg)
                        logging.error(error_msg)
                except sqlite3.IntegrityError:
                     failed_recoveries += 1
                     item_desc = f"item on line {line_num + 1} (Path: {item_data.get('symlink_path', 'Unknown')})"
                     error_msg = f"RIVEN: Skipped recovery for {item_desc}: Item likely already exists in DB (UNIQUE constraint violation)."
                     errors.append(error_msg)
                     logging.warning(error_msg)

            except json.JSONDecodeError as json_err:
                 failed_recoveries += 1
                 error_msg = f"RIVEN: Failed to parse JSON on line {line_num + 1}: {json_err}"
                 errors.append(error_msg)
                 logging.error(error_msg)
            except ValueError as val_err:
                failed_recoveries += 1
                item_desc = f"item on line {line_num + 1} (Path: {item_data.get('symlink_path', 'Unknown') if item_data else 'Unknown'})"
                error_msg = f"RIVEN: Validation error for {item_desc}: {val_err}"
                errors.append(error_msg)
                logging.error(error_msg)
            except Exception as e:
                failed_recoveries += 1
                item_desc = f"item on line {line_num + 1} (Path: {item_data.get('symlink_path', 'Unknown') if item_data else 'Unknown'})"
                error_msg = f"RIVEN: Failed to recover {item_desc}: {str(e)}"
                errors.append(error_msg)
                logging.error(error_msg, exc_info=True)

        logging.info(f"RIVEN: Recovery processing complete for task {task_id}. Total successful: {successful_recoveries}, Failed/Skipped: {failed_recoveries}")

    except Exception as outer_err:
        error_msg = f"RIVEN: Error during recovery process: {str(outer_err)}"
        errors.append(error_msg)
        logging.error(error_msg, exc_info=True)
        failed_recoveries = expected_items - successful_recoveries
    finally:
        if recovery_file:
            try:
                recovery_file.close()
            except Exception as close_err:
                 logging.error(f"RIVEN: Error closing recovery file {recovery_file_path}: {close_err}")
        
        if recovery_file_path and os.path.exists(recovery_file_path) and not errors:
            try:
                os.remove(recovery_file_path)
                logging.info(f"RIVEN: Successfully deleted recovery file: {recovery_file_path}")
            except Exception as del_err:
                logging.error(f"RIVEN: Failed to delete recovery file {recovery_file_path}: {del_err}")
                errors.append(f"Note: RIVEN: Failed to automatically delete recovery file {os.path.basename(recovery_file_path)}. Please delete it manually.")
        elif errors:
             logging.warning(f"RIVEN: Recovery file {recovery_file_path} was not deleted due to errors during the recovery process.")
             errors.append(f"Note: RIVEN: Recovery file {os.path.basename(recovery_file_path)} was kept due to errors. Please review and delete it manually.")

    return jsonify({
        'success': failed_recoveries == 0,
        'successful_recoveries': successful_recoveries,
        'failed_recoveries': failed_recoveries,
        'errors': errors
    })

# --- End Riven Symlink Recovery Routes ---
# --- Symlink Path Modification ---

@debug_bp.route('/api/resync_symlinks_trigger', methods=['POST'])
@admin_required
def resync_symlinks_route():
    logging.info("Attempting to resync symlinks with current settings.")

    try:
        # This function logs its own progress and errors.
        # It's a potentially long-running synchronous operation.
        # Call the underlying function without the optional path arguments
        resync_symlinks_with_new_settings(
            old_original_files_path_setting=None,
            new_original_files_path_setting=None
        )
        # The function itself handles logging. The UI will show this generic success message.
        return jsonify({'success': True, 'message': 'Symlink resynchronization process initiated. Check server logs for details and progress.'})
    except Exception as e:
        logging.error(f"Error during symlink resynchronization trigger: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': f'An error occurred: {str(e)}'}), 500
