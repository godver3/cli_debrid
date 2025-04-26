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
from routes.notifications import send_notifications
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
from utilities.plex_functions import get_collected_from_plex
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

debug_bp = Blueprint('debug', __name__)

# Global progress tracking
scan_progress = {}

# Global dictionary to store analysis progress
analysis_progress = {}

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
            if item_dict['type'] == 'episode':
                item_dict['title'] = f"{item_dict['title']} S{item_dict['season_number']:02d}E{item_dict['episode_number']:02d}"
            elif item_dict['type'] == 'movie':
                item_dict['title'] = f"{item_dict['title']} ({item_dict['year']})"
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
    if request.method == 'POST':
        action = request.form.get('action')
        imdb_id = request.form.get('imdb_id')
        
        if not imdb_id:
            flash('IMDb ID is required', 'error')
            return redirect(url_for('debug.manual_blacklist'))

        blacklist = get_manual_blacklist()
        
        if action == 'add':
            try:
                # Try TV show metadata first
                direct_api = DirectAPI()
                show_data, _ = direct_api.get_show_metadata(imdb_id)
                if show_data:
                    if isinstance(show_data, str):
                        show_data = json.loads(show_data)
                        
                    add_to_manual_blacklist(
                        imdb_id=imdb_id,
                        media_type='episode',
                        title=show_data.get('title', 'Unknown Title'),
                        year=str(show_data.get('year', '')),
                        season=None  # Initially add with no seasons selected
                    )
                    flash('Successfully added to blacklist', 'success')
                else:
                    # If not a TV show, try movie metadata
                    movie_data, _ = direct_api.get_movie_metadata(imdb_id)
                    if movie_data:
                        if isinstance(movie_data, str):
                            movie_data = json.loads(movie_data)
                        add_to_manual_blacklist(
                            imdb_id=imdb_id,
                            media_type='movie',
                            title=movie_data.get('title', 'Unknown Title'),
                            year=str(movie_data.get('year', '')),
                        )
                        flash('Successfully added to blacklist', 'success')
                    else:
                        flash('Unable to fetch metadata for IMDb ID', 'error')
            except Exception as e:
                flash(f'Error adding to blacklist: {str(e)}', 'error')
                logging.error(f"Error adding to blacklist: {str(e)}", exc_info=True)
                
        elif action == 'update_seasons':
            try:
                if imdb_id in blacklist:
                    item = blacklist[imdb_id]
                    if item['media_type'] == 'episode':
                        # Check if all seasons is selected
                        all_seasons = request.form.get('all_seasons') == 'on'
                        
                        if all_seasons:
                            item['seasons'] = []  # Empty list means all seasons
                        else:
                            # Get selected seasons from form
                            selected_seasons = request.form.getlist('seasons')
                            # Convert to integers and sort
                            item['seasons'] = sorted([int(s) for s in selected_seasons])
                            
                        save_manual_blacklist(blacklist)
                        flash('Successfully updated seasons', 'success')
                    else:
                        flash('Only TV shows can have seasons updated', 'error')
                else:
                    flash('Show not found in blacklist', 'error')
            except Exception as e:
                flash(f'Error updating seasons: {str(e)}', 'error')
                logging.error(f"Error updating seasons: {str(e)}", exc_info=True)
                
        elif action == 'remove':
            try:
                remove_from_manual_blacklist(imdb_id)
                flash('Successfully removed from blacklist', 'success')
            except Exception as e:
                flash(f'Error removing from blacklist: {str(e)}', 'error')
    
    # Get blacklist and sort by title
    blacklist = get_manual_blacklist()
    
    # Add error handling for sorting
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
    
    # Fetch season information for TV shows
    direct_api = DirectAPI()
    for imdb_id, item in sorted_blacklist.items():
        if item['media_type'] == 'episode':
            try:
                seasons_data, _ = direct_api.get_show_seasons(imdb_id)
                if seasons_data:
                    logging.debug(f"Seasons data for {imdb_id}: {seasons_data}")
                    if isinstance(seasons_data, str):
                        seasons_data = json.loads(seasons_data)
                        
                    # Handle the new format where seasons are direct keys
                    if isinstance(seasons_data, dict) and all(str(k).isdigit() for k in seasons_data.keys()):
                        item['available_seasons'] = sorted([int(season) for season in seasons_data.keys()])
                        # Also store episode counts
                        item['season_episodes'] = {int(season): data.get('episode_count', 0) for season, data in seasons_data.items()}
                    # Keep backward compatibility for the old format
                    else:
                        item['available_seasons'] = sorted([int(s['season_number']) for s in seasons_data.get('seasons', [])])
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
def get_collected_from_plex():
    from routes.extensions import task_queue

    collection_type = request.form.get('collection_type')
    
    if collection_type not in ['all', 'recent']:
        return jsonify({'success': False, 'error': 'Invalid collection type'}), 400

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
    from content_checkers.trakt import get_wanted_from_trakt_lists, get_wanted_from_trakt_watchlist, get_wanted_from_trakt_collection, get_wanted_from_friend_trakt_watchlist
    from content_checkers.mdb_list import get_wanted_from_mdblists
    from content_checkers.content_source_detail import append_content_source_detail
    from metadata.metadata import process_metadata

    content_sources = get_all_settings().get('Content Sources', {})
    source_data = content_sources.get(source_id) # Use .get for safety
    if not source_data:
        logging.error(f"Source ID {source_id} not found in settings.")
        return {'added': 0, 'processed': 0, 'cache_skipped': 0, 'media_type_skipped': 0, 'error': f"Source {source_id} not found"}

    source_type = source_id.split('_')[0]
    versions = source_data.get('versions', {})
    source_media_type = source_data.get('media_type', 'All')

    logging.info(f"Processing source: {source_id}")
    logging.debug(f"Source type: {source_type}, media type: {source_media_type}")
    
    source_cache = load_source_cache(source_id)
    logging.debug(f"Initial cache state for {source_id}: {len(source_cache)} entries")
    cache_skipped = 0
    items_processed = 0
    total_items_added = 0 # Renamed for clarity
    media_type_skipped = 0

    wanted_content = []
    try: # Add try block for source fetching
        if source_type == 'Overseerr':
            wanted_content = get_wanted_from_overseerr(versions)
        elif source_type == 'My Plex Watchlist':
            wanted_content = get_wanted_from_plex_watchlist(versions)
        elif source_type == 'My Plex RSS Watchlist':
            plex_rss_url = source_data.get('url', '')
            if not plex_rss_url:
                logging.error(f"Missing URL for source: {source_id}")
                return {'added': 0, 'processed': 0, 'cache_skipped': 0, 'media_type_skipped': 0, 'error': f"Missing URL for {source_id}"}
            wanted_content = get_wanted_from_plex_rss(plex_rss_url, versions)
        elif source_type == 'My Friends Plex RSS Watchlist':
            plex_rss_url = source_data.get('url', '')
            if not plex_rss_url:
                logging.error(f"Missing URL for source: {source_id}")
                return {'added': 0, 'processed': 0, 'cache_skipped': 0, 'media_type_skipped': 0, 'error': f"Missing URL for {source_id}"}
            wanted_content = get_wanted_from_friends_plex_rss(plex_rss_url, versions)
        elif source_type == 'Other Plex Watchlist':
            wanted_content = get_wanted_from_other_plex_watchlist(
                username=source_data.get('username', ''),
                token=source_data.get('token', ''),
                versions=versions
            )
        elif source_type == 'MDBList':
            mdblist_urls = source_data.get('urls', '').split(',')
            for mdblist_url in mdblist_urls:
                mdblist_url = mdblist_url.strip()
                if mdblist_url: # Check if url is not empty
                    wanted_content.extend(get_wanted_from_mdblists(mdblist_url, versions))
        elif source_type == 'Trakt Watchlist':
            update_trakt_settings(content_sources)
            wanted_content = get_wanted_from_trakt_watchlist(versions)
        elif source_type == 'Trakt Lists':
            update_trakt_settings(content_sources)
            trakt_lists = source_data.get('trakt_lists', '').split(',')
            for trakt_list in trakt_lists:
                trakt_list = trakt_list.strip()
                if trakt_list: # Check if list name is not empty
                    wanted_content.extend(get_wanted_from_trakt_lists(trakt_list, versions))
        elif source_type == 'Friends Trakt Watchlist':
            update_trakt_settings(content_sources)
            wanted_content = get_wanted_from_friend_trakt_watchlist(source_data, versions)
        elif source_type == 'Trakt Collection':
            update_trakt_settings(content_sources)
            wanted_content = get_wanted_from_trakt_collection(versions)
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
                for items, item_versions in wanted_content:
                    batch_items_processed = 0
                    batch_total_items_added = 0
                    batch_cache_skipped = 0
                    batch_media_type_skipped = 0

                    try:
                        logging.debug(f"Processing batch of {len(items)} items from {source_id}")

                        original_count = len(items)
                        # Filter by media type
                        if source_media_type != 'All' and not source_type.startswith('Collected'):
                            items = [
                                item for item in items
                                if (source_media_type == 'Movies' and item.get('media_type') == 'movie') or
                                   (source_media_type == 'Shows' and item.get('media_type') == 'tv')
                            ]
                            batch_media_type_skipped += original_count - len(items)
                            if batch_media_type_skipped > 0:
                                logging.debug(f"Batch {source_id}: Skipped {batch_media_type_skipped} items due to media type mismatch")

                        # Filter by cache
                        items_to_process = [
                            item for item in items
                            if should_process_item(item, source_id, source_cache)
                        ]
                        batch_cache_skipped += len(items) - len(items_to_process)
                        logging.debug(f"Batch {source_id}: Cache filtering results: {batch_cache_skipped} skipped, {len(items_to_process)} to process")

                        if items_to_process:
                            batch_items_processed += len(items_to_process)
                            processed_items_meta = process_metadata(items_to_process)
                            if processed_items_meta:
                                all_items_meta = processed_items_meta.get('movies', []) + processed_items_meta.get('episodes', [])
                                for item in all_items_meta:
                                    item['content_source'] = source_id
                                    item = append_content_source_detail(item, source_type=source_type)

                                for item_original in items_to_process:
                                    update_cache_for_item(item_original, source_id, source_cache)

                                from database import add_wanted_items
                                added_count = add_wanted_items(all_items_meta, item_versions or versions) # Assuming add_wanted_items returns count
                                batch_total_items_added += added_count or 0

                    except Exception as batch_error:
                        logging.error(f"Error processing batch from {source_id}: {str(batch_error)}", exc_info=True)
                        # Continue to next batch

                    # Aggregate results from batch
                    items_processed += batch_items_processed
                    total_items_added += batch_total_items_added
                    cache_skipped += batch_cache_skipped
                    media_type_skipped += batch_media_type_skipped

            else: # Handle single list of items (assuming this path is less common based on previous logic)
                original_count = len(wanted_content)
                # Filter by media type
                if source_media_type != 'All' and not source_type.startswith('Collected'):
                    wanted_content = [
                        item for item in wanted_content
                        if (source_media_type == 'Movies' and item.get('media_type') == 'movie') or
                           (source_media_type == 'Shows' and item.get('media_type') == 'tv')
                    ]
                    media_type_skipped += original_count - len(wanted_content)
                    if media_type_skipped > 0:
                        logging.debug(f"{source_id}: Skipped {media_type_skipped} items due to media type mismatch")

                # Filter by cache
                items_to_process = [
                    item for item in wanted_content
                    if should_process_item(item, source_id, source_cache)
                ]
                cache_skipped += len(wanted_content) - len(items_to_process)
                logging.debug(f"{source_id}: Cache filtering results: {cache_skipped} skipped, {len(items_to_process)} to process")

                if items_to_process:
                    items_processed += len(items_to_process)
                    processed_items_meta = process_metadata(items_to_process)
                    if processed_items_meta:
                        all_items_meta = processed_items_meta.get('movies', []) + processed_items_meta.get('episodes', [])
                        for item in all_items_meta:
                            item['content_source'] = source_id
                            item = append_content_source_detail(item, source_type=source_type)

                        for item_original in items_to_process:
                            update_cache_for_item(item_original, source_id, source_cache)

                        from database import add_wanted_items
                        added_count = add_wanted_items(all_items_meta, versions) # Assuming add_wanted_items returns count
                        total_items_added += added_count or 0

            # Save the updated cache
            save_source_cache(source_id, source_cache)
            logging.debug(f"Final cache state for {source_id}: {len(source_cache)} entries")

            stats_msg = f"Source {source_id}: Added {total_items_added} items"
            if items_processed > 0: stats_msg += f" (Processed {items_processed} items)"
            if cache_skipped > 0: stats_msg += f", Skipped {cache_skipped} (cache)"
            if media_type_skipped > 0: stats_msg += f", Skipped {media_type_skipped} (media type)"
            logging.info(stats_msg)

        except Exception as process_error:
            logging.error(f"Error processing items from {source_id}: {str(process_error)}", exc_info=True)
            # Return counts accumulated so far, plus the error
            return {'added': total_items_added, 'processed': items_processed, 'cache_skipped': cache_skipped, 'media_type_skipped': media_type_skipped, 'error': f"Error processing items: {str(process_error)}"}

    else:
        logging.info(f"No wanted content retrieved from {source_id}")

    # Return the final counts
    return {'added': total_items_added, 'processed': items_processed, 'cache_skipped': cache_skipped, 'media_type_skipped': media_type_skipped}

def get_content_sources():
    """Get content sources from ProgramRunner instance."""
    program_runner = ProgramRunner()
    return program_runner.get_content_sources()

@debug_bp.route('/api/get_wanted_content', methods=['POST'])
def get_wanted_content():
    from routes.extensions import task_queue

    source = request.form.get('source')
    task_id = task_queue.add_task(async_get_wanted_content, source)
    return jsonify({'task_id': task_id}), 202

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
def rescrape_item():
    from database import get_media_item_by_id
    from utilities.plex_functions import remove_file_from_plex
    from metadata.metadata import process_metadata
    item_id = request.json.get('item_id')
    if not item_id:
        return jsonify({'success': False, 'error': 'Item ID is required'}), 400

    try:
        # Get the item details first
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

            time.sleep(1)

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

            time.sleep(1)

            # Remove from Plex if configured
            plex_url = get_setting('File Management', 'plex_url_for_symlink', '')
            if plex_url:
                if item['type'] == 'movie':
                    remove_file_from_plex(item['title'], os.path.basename(item['location_on_disk']))
                elif item['type'] == 'episode':
                    remove_file_from_plex(item['title'], os.path.basename(item['location_on_disk']), item['episode_title'])

        # Move the item to Wanted queue
        move_item_to_wanted(item_id)
        return jsonify({'success': True, 'message': 'Item deleted and moved to Wanted queue for rescraping'}), 200
    except Exception as e:
        logging.error(f"Error rescraping item: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

def move_item_to_wanted(item_id):
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
                original_scraped_torrent_title = NULL,
                upgrading_from = NULL,
                version = TRIM(version, '*'),
                upgrading = NULL
            WHERE id = ?
        ''', (datetime.now(), item_id))
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
    task_name = request.json.get('task_name')
    if not task_name:
        return jsonify({'success': False, 'error': 'Task name is required'}), 400

    program_runner = ProgramRunner() # Get the singleton instance

    # --- START: Use trigger_task instead of direct execution ---
    try:
        # Use the trigger_task method which handles state management
        # The method already normalizes the name internally
        program_runner.trigger_task(task_name)

        # Format display name (can reuse logic if needed, but trigger_task handles execution)
        display_name = task_name # Keep it simple for now or reuse formatting logic
        try:
            # Attempt nice formatting for the message
             normalized_name = program_runner._normalize_task_name(task_name)
             if normalized_name.startswith('task_'):
                 display_name = ' '.join(word.capitalize() for word in normalized_name.replace('task_', '').split('_'))
             elif normalized_name in program_runner.queue_processing_map:
                 display_name = normalized_name.capitalize()
             # Add content source display name logic if desired
        except Exception:
            display_name = task_name # Fallback

        return jsonify({'success': True, 'message': f'Task "{display_name}" triggered successfully'}), 200
    except ValueError as ve: # Catch specific errors from trigger_task
         logging.warning(f"Failed to trigger task '{task_name}': {ve}")
         return jsonify({'success': False, 'error': str(ve)}), 400 # Return specific error
    except RuntimeError as re: # Catch the wrapped execution error from trigger_task
        logging.error(f"Error executing triggered task {task_name}: {re}", exc_info=True)
        # Extract original error if possible, otherwise use RuntimeError message
        error_msg = str(re.__cause__) if re.__cause__ else str(re)
        return jsonify({'success': False, 'error': f'Error executing task "{task_name}": {error_msg}'}), 500
    except Exception as e:
        logging.error(f"Unexpected error triggering task {task_name}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': f'Unexpected error triggering task "{task_name}": {str(e)}'}), 500
    # --- END: Use trigger_task ---

    # --- REMOVE OLD DIRECT EXECUTION LOGIC ---
    # queue_manager = QueueManager()
    # tasks = { ... } # Dictionary no longer needed here
    # if task_name not in tasks:
    #     return jsonify({'success': False, 'error': 'Invalid task name'}), 400
    # try:
    #     result = tasks[task_name]()
    #     return jsonify({'success': True, 'message': f'Task "{display_name}" executed successfully', 'result': result}), 200
    # except Exception as e:
    #     logging.error(f"Error executing task {task_name}: {str(e)}")
    #     return jsonify({'success': False, 'error': f'Error executing task "{display_name}": {str(e)}'}), 500
    # --- END REMOVED LOGIC ---

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
        {'id': 'final_check_queue', 'display_name': 'Final Check Queue'}
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
def not_wanted():
    """Display not wanted magnets and URLs."""
    magnets = get_not_wanted_magnets()
    urls = get_not_wanted_urls()
    return render_template('debug_not_wanted.html', magnets=magnets, urls=urls)

@debug_bp.route('/not_wanted/magnet/remove', methods=['POST'])
def remove_not_wanted_magnet():
    """Remove a magnet from the not wanted list."""
    magnet = request.form.get('magnet')
    if magnet:
        magnets = get_not_wanted_magnets()
        if magnet in magnets:
            magnets.remove(magnet)
            save_not_wanted_magnets(magnets)
            flash('Magnet removed from not wanted list.', 'success')
        else:
            flash('Magnet not found in not wanted list.', 'error')
    return redirect(url_for('debug.not_wanted'))

@debug_bp.route('/not_wanted/url/remove', methods=['POST'])
def remove_not_wanted_url():
    """Remove a URL from the not wanted list."""
    url = request.form.get('url')
    if url:
        urls = get_not_wanted_urls()
        if url in urls:
            urls.remove(url)
            save_not_wanted_urls(urls)
            flash('URL removed from not wanted list.', 'success')
        else:
            flash('URL not found in not wanted list.', 'error')
    return redirect(url_for('debug.not_wanted'))

@debug_bp.route('/not_wanted/purge', methods=['POST'])
def purge_not_wanted():
    """Purge all not wanted magnets and URLs."""
    try:
        # Create empty sets for both magnets and URLs
        save_not_wanted_magnets(set())
        save_not_wanted_urls(set())
        flash('All not wanted items have been purged.', 'success')
    except Exception as e:
        flash(f'Error purging not wanted items: {str(e)}', 'error')
    return redirect(url_for('debug.not_wanted'))

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
                from utilities.local_library_scan import convert_item_to_symlink
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

                    result = convert_item_to_symlink(item_dict)

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
            return jsonify({'status': 'error', 'message': 'Failed to retrieve data from Emby/Jellyfin.'}), 500

        movies = collected_data.get('movies', [])
        episodes = collected_data.get('episodes', [])
        combined_items = movies + episodes

        logging.info(f"Retrieved {len(movies)} movies and {len(episodes)} episodes from Emby/Jellyfin.")

        if not combined_items:
             logging.warning("No items collected from Emby/Jellyfin scan.")
             return jsonify({'status': 'success', 'message': 'No items collected from Emby/Jellyfin scan.'}), 200

        # 2. Add collected items to the database
        logging.info("Adding collected Emby/Jellyfin items to the database...")
        add_collected_items(combined_items, recent=True) # Change to recent=True for additive only
        logging.info("Successfully added Emby/Jellyfin items to the database.")

        return jsonify({'success': True, 'message': f'Successfully processed {len(combined_items)} items from Emby/Jellyfin.'}), 200

    except Exception as e:
        logging.error(f"Error during direct Emby/Jellyfin scan: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'message': f'An error occurred: {str(e)}'}), 500 # Also change error response for consistency

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
        symlink_organize_by_resolution = get_setting('File Management', 'symlink_organize_by_resolution', False) # Read the setting
        separate_anime = get_setting('Debug', 'enable_separate_anime_folders')
        movies_folder = get_setting('Debug', 'movies_folder_name', 'Movies')
        tv_shows_folder = get_setting('Debug', 'tv_shows_folder_name', 'TV Shows')
        anime_movies_folder = get_setting('Debug', 'anime_movies_folder_name', 'Anime Movies') if separate_anime else None
        anime_tv_shows_folder = get_setting('Debug', 'anime_tv_shows_folder_name', 'Anime TV Shows') if separate_anime else None

        ignored_extensions = {'.srt', '.sub', '.idx', '.nfo', '.txt', '.jpg', '.png', '.db', '.partial', '.!qB'}
        
        # Define base type folders
        type_folders = [movies_folder, tv_shows_folder]
        if separate_anime:
            if anime_movies_folder: type_folders.append(anime_movies_folder)
            if anime_tv_shows_folder: type_folders.append(anime_tv_shows_folder)

        # Define potential resolution folders
        resolution_folders = ["2160p", "1080p"] if symlink_organize_by_resolution else [None] # Add None for the non-resolution case

        total_items_scanned = 0
        total_symlinks_processed = 0
        total_files_processed = 0
        items_found = 0
        parser_errors = 0
        metadata_errors = 0
        recoverable_items_preview = []

        update_progress(status='scanning', message='Starting directory scan...')

        # Iterate through potential resolution folders (will be just [None] if setting is off)
        for res_folder in resolution_folders:
            # Iterate through type folders
            for type_folder in type_folders:
                
                # Construct the path to scan based on whether resolution folders are used
                if res_folder:
                    current_search_path = symlink_root_path / res_folder / type_folder
                    scan_target_name = f'{res_folder}/{type_folder}'
                else:
                    current_search_path = symlink_root_path / type_folder
                    scan_target_name = type_folder

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
                    except Exception as e:
                        logging.error(f"Error scanning {current_search_path}: {e}", exc_info=True)
                        update_progress(status='error', message=f'Error scanning {scan_target_name}: {e}')
                        # Continue to next folder on error
                else:
                    logging.warning(f"Directory not found or not accessible: {current_search_path}")
                    
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
        # Ensure the recovery file is closed
        if recovery_file:
            try:
                recovery_file.close()
            except Exception as close_err:
                 logging.error(f"Error closing recovery file {recovery_file_path}: {close_err}")
        
        # Clean up the progress entry from memory after 5 minutes (to allow recovery)
        # But only remove if it wasn't an error or if recovery isn't needed?
        # Let's keep it for now for potential debugging. Consider cleanup strategy later.
        # threading.Timer(300, lambda: analysis_progress.pop(task_id, None)).start()
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

                now_iso = datetime.now().isoformat()

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
