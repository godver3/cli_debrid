from flask import jsonify, Blueprint, render_template, request, redirect, url_for, flash, current_app, Response, stream_with_context
from flask.json import jsonify
from initialization import get_all_wanted_from_enabled_sources
from run_program import (
    get_and_add_recent_collected_from_plex, 
    get_and_add_all_collected_from_plex, 
    ProgramRunner, 
    run_local_library_scan, 
    run_recent_local_library_scan
)
from manual_blacklist import add_to_manual_blacklist, remove_from_manual_blacklist, get_manual_blacklist, save_manual_blacklist
from settings import get_all_settings, get_setting, set_setting
from config_manager import load_config
import logging
from routes import admin_required
from content_checkers.overseerr import get_wanted_from_overseerr
from content_checkers.collected import get_wanted_from_collected
from content_checkers.plex_watchlist import get_wanted_from_plex_watchlist, get_wanted_from_other_plex_watchlist
from content_checkers.plex_rss_watchlist import get_wanted_from_plex_rss, get_wanted_from_friends_plex_rss
from content_checkers.trakt import get_wanted_from_trakt_lists, get_wanted_from_trakt_watchlist, get_wanted_from_trakt_collection, get_wanted_from_friend_trakt_watchlist
from content_checkers.mdb_list import get_wanted_from_mdblists
from content_checkers.content_source_detail import append_content_source_detail
from metadata.metadata import process_metadata, get_metadata
from cli_battery.app.direct_api import DirectAPI
from database import add_wanted_items, get_db_connection, bulk_delete_by_id, create_tables, verify_database
from database.torrent_tracking import get_recent_additions, get_torrent_history
import os
import glob
from api_tracker import api 
import time
from metadata.metadata import get_tmdb_id_and_media_type, refresh_release_dates
from datetime import datetime
from notifications import send_notifications
import requests
from datetime import datetime, timedelta
from queue_manager import QueueManager
from not_wanted_magnets import (
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

debug_bp = Blueprint('debug', __name__)

# Global progress tracking
scan_progress = {}

def async_get_wanted_content(source):
    try:
        if source == 'all':
            get_all_wanted_from_enabled_sources()
            message = 'Successfully retrieved and added wanted items from all enabled sources'
        else:
            get_and_add_wanted_content(source)
            message = f'Successfully retrieved and added wanted items from {source}'
        return {'success': True, 'message': message}
    except Exception as e:
        return {'success': False, 'error': str(e)}

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
    return render_template('debug_functions.html', content_sources=enabled_sources)

@debug_bp.route('/bulk_delete_by_imdb', methods=['POST'])
def bulk_delete_by_imdb():
    id_value = request.form.get('imdb_id')
    if not id_value:
        return jsonify({'success': False, 'error': 'ID is required'})

    id_type = 'imdb_id' if id_value.startswith('tt') else 'tmdb_id'
    deleted_count = bulk_delete_by_id(id_value, id_type)
    
    if deleted_count > 0:
        return jsonify({'success': True, 'message': f'Successfully deleted {deleted_count} items with {id_type.upper()}: {id_value}'})
    else:
        return jsonify({'success': False, 'error': f'No items found with {id_type.upper()}: {id_value}'})

@debug_bp.route('/refresh_release_dates', methods=['POST'])
@admin_required
def refresh_release_dates_route():
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

@debug_bp.route('/api/get_title_year', methods=['GET'])
def get_title_year():
    imdb_id = request.args.get('imdb_id')
    
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    
    if not overseerr_url or not overseerr_api_key:
        return jsonify({'error': 'Overseerr URL or API key not set'}), 400

    cookies = get_overseerr_cookies(overseerr_url)
    tmdb_id, media_type = get_tmdb_id_and_media_type(imdb_id)
    
    if tmdb_id and media_type:
        if media_type == 'movie':
            details = get_overseerr_movie_details(overseerr_url, overseerr_api_key, tmdb_id, cookies)
        else:  # TV show
            details = get_overseerr_show_details(overseerr_url, overseerr_api_key, tmdb_id, cookies)
        
        if details:
            title = details.get('title') if media_type == 'movie' else details.get('name')
            year = details.get('releaseDate', '')[:4] if media_type == 'movie' else details.get('firstAirDate', '')[:4]
            return jsonify({'title': title, 'year': year, 'media_type': media_type})

    return jsonify({'error': 'Could not fetch title and year'}), 404

@debug_bp.route('/api/get_collected_from_plex', methods=['POST'])
def get_collected_from_plex():
    from extensions import task_queue

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
            update = {
                'status': status_type,
                'message': message,
                'complete': status_type in ['complete', 'error']
            }
            if counts:
                update.update(counts)
            scan_progress[scan_id].update(update)
            
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
                scan_progress[scan_id].update({
                    'status': 'error',
                    'message': f"Error during scan: {error_msg}",
                    'success': False,
                    'complete': True,
                    'phase': 'error',
                    'errors': [error_msg]
                })
            finally:
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
        while True:
            if scan_id not in scan_progress:
                yield f"data: {json.dumps({'status': 'error', 'message': 'Scan not found'})}\n\n"
                break
                
            progress = scan_progress[scan_id]
            
            # Add error details to progress if any exist
            if progress.get('errors'):
                progress['error_details'] = progress['errors']
            
            yield f"data: {json.dumps(progress)}\n\n"
            
            if progress['complete']:
                break
                
            time.sleep(1)
    
    return Response(stream_with_context(generate()), mimetype='text/event-stream')

@debug_bp.route('/api/task_status/<task_id>')
def task_status(task_id):
    from extensions import task_queue

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
    content_sources = get_all_settings().get('Content Sources', {})
    source_data = content_sources[source_id]
    source_type = source_id.split('_')[0]
    versions = source_data.get('versions', {})
    source_media_type = source_data.get('media_type', 'All')

    logging.info(f"Processing source: {source_id}")
    logging.debug(f"Source type: {source_type}, media type: {source_media_type}")
    
    # Load cache for this source
    source_cache = load_source_cache(source_id)
    logging.debug(f"Initial cache state for {source_id}: {len(source_cache)} entries")
    cache_skipped = 0
    items_processed = 0
    total_items = 0
    media_type_skipped = 0

    wanted_content = []
    if source_type == 'Overseerr':
        wanted_content = get_wanted_from_overseerr(versions)
    elif source_type == 'My Plex Watchlist':
        wanted_content = get_wanted_from_plex_watchlist(versions)
    elif source_type == 'My Plex RSS Watchlist':
        plex_rss_url = source_data.get('url', '')
        if not plex_rss_url:
            logging.error(f"Missing URL for source: {source_id}")
            return
        wanted_content = get_wanted_from_plex_rss(plex_rss_url, versions)
    elif source_type == 'My Friends Plex RSS Watchlist':
        plex_rss_url = source_data.get('url', '')
        if not plex_rss_url:
            logging.error(f"Missing URL for source: {source_id}")
            return
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
            wanted_content.extend(get_wanted_from_mdblists(mdblist_url, versions))
    elif source_type == 'Trakt Watchlist':
        update_trakt_settings(content_sources)
        wanted_content = get_wanted_from_trakt_watchlist(versions)
    elif source_type == 'Trakt Lists':
        update_trakt_settings(content_sources)
        trakt_lists = source_data.get('trakt_lists', '').split(',')
        for trakt_list in trakt_lists:
            trakt_list = trakt_list.strip()
            wanted_content.extend(get_wanted_from_trakt_lists(trakt_list, versions))
    elif source_type == 'Friends Trakt Watchlist':
        update_trakt_settings(content_sources)
        wanted_content = get_wanted_from_friend_trakt_watchlist(source_data, versions)
    elif source_type == 'Trakt Collection':
        update_trakt_settings(content_sources)
        wanted_content = get_wanted_from_trakt_collection(versions)
    elif source_type == 'Collected':
        wanted_content = get_wanted_from_collected()

    logging.debug(f"wanted_content for {source_id}: {wanted_content}")

    if wanted_content:
        total_items = 0
        if isinstance(wanted_content, list) and len(wanted_content) > 0 and isinstance(wanted_content[0], tuple):
            # Handle list of tuples
            for items, item_versions in wanted_content:
                try:
                    logging.debug(f"Processing batch of {len(items)} items from {source_id}")
                    
                    # Filter items by media type first if applicable
                    if source_media_type != 'All' and not source_type.startswith('Collected'):
                        original_count = len(items)
                        items = [
                            item for item in items
                            if (source_media_type == 'Movies' and item.get('media_type') == 'movie') or
                               (source_media_type == 'Shows' and item.get('media_type') == 'tv')
                        ]
                        media_type_skipped += original_count - len(items)
                        if media_type_skipped > 0:
                            logging.debug(f"Skipped {media_type_skipped} items due to media type mismatch")
                    
                    # Filter items based on cache before metadata processing
                    items_to_process = [
                        item for item in items 
                        if should_process_item(item, source_id, source_cache)
                    ]
                    items_skipped = len(items) - len(items_to_process)
                    cache_skipped += items_skipped
                    logging.debug(f"Cache filtering results for {source_id}: {items_skipped} skipped, {len(items_to_process)} to process")
                    
                    if not items_to_process:
                        continue

                    # Process metadata for non-cached items
                    processed_items = process_metadata(items_to_process)
                    if processed_items:
                        all_items = processed_items.get('movies', []) + processed_items.get('episodes', [])
                        # Add content source and detail
                        for item in all_items:
                            item['content_source'] = source_id
                            item = append_content_source_detail(item, source_type=source_type)
                        
                        # Update cache for the original items (pre-metadata processing)
                        for item in items_to_process:
                            update_cache_for_item(item, source_id, source_cache)
                        
                        add_wanted_items(all_items, item_versions or versions)
                        total_items += len(all_items)
                        items_processed += len(items_to_process)
                except Exception as e:
                    logging.error(f"Error processing items from {source_id}: {str(e)}")
                    logging.error(traceback.format_exc())
        
        # Save the updated cache
        save_source_cache(source_id, source_cache)
        logging.debug(f"Final cache state for {source_id}: {len(source_cache)} entries")
        
        stats_msg = f"Added {total_items} wanted items from {source_id} (processed {items_processed} items"
        if cache_skipped > 0:
            stats_msg += f", skipped {cache_skipped} cached items"
        if media_type_skipped > 0:
            stats_msg += f", skipped {media_type_skipped} items due to media type mismatch"
        stats_msg += ")"
        logging.info(stats_msg)
    else:
        logging.warning(f"No wanted content retrieved from {source_id}")

def get_content_sources():
    """Get content sources from ProgramRunner instance."""
    program_runner = ProgramRunner()
    return program_runner.get_content_sources()

@debug_bp.route('/api/get_wanted_content', methods=['POST'])
def get_wanted_content():
    from extensions import task_queue

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
    item_id = request.json.get('item_id')
    if not item_id:
        return jsonify({'success': False, 'error': 'Item ID is required'}), 400

    try:
        move_item_to_wanted(item_id)
        return jsonify({'success': True, 'message': 'Item moved to Wanted queue for rescraping'}), 200
    except Exception as e:
        logging.error(f"Error moving item to Wanted queue: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

def move_item_to_wanted(item_id):
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

    program_runner = ProgramRunner()
    queue_manager = QueueManager()

    tasks = {
        'wanted': queue_manager.process_wanted,
        'scraping': queue_manager.process_scraping,
        'adding': queue_manager.process_adding,
        'checking': queue_manager.process_checking,
        'sleeping': queue_manager.process_sleeping,
        'unreleased': queue_manager.process_unreleased,
        'blacklisted': queue_manager.process_blacklisted,
        'pending_uncached': queue_manager.process_pending_uncached,
        'upgrading': queue_manager.process_upgrading,
        'task_plex_full_scan': program_runner.task_plex_full_scan,
        'task_debug_log': program_runner.task_debug_log,
        'task_refresh_release_dates': program_runner.task_refresh_release_dates,
        'task_purge_not_wanted_magnets_file': program_runner.task_purge_not_wanted_magnets_file,
        'task_generate_airtime_report': program_runner.task_generate_airtime_report,
        'task_check_service_connectivity': program_runner.task_check_service_connectivity,
        'task_send_notifications': program_runner.task_send_notifications,
        'task_check_trakt_early_releases': program_runner.task_check_trakt_early_releases,
        'task_reconcile_queues': program_runner.task_reconcile_queues,
        'task_check_plex_files': program_runner.task_check_plex_files,
        'task_update_show_ids': program_runner.task_update_show_ids,
        'task_update_show_titles': program_runner.task_update_show_titles,
        'task_update_movie_ids': program_runner.task_update_movie_ids,
        'task_update_movie_titles': program_runner.task_update_movie_titles,
        'task_get_plex_watch_history': program_runner.task_get_plex_watch_history,
        'task_check_database_health': program_runner.task_check_database_health,
        'task_run_library_maintenance': program_runner.task_run_library_maintenance,
        'task_verify_symlinked_files': program_runner.task_verify_symlinked_files,
    }

    if task_name not in tasks:
        return jsonify({'success': False, 'error': 'Invalid task name'}), 400

    try:
        result = tasks[task_name]()
        return jsonify({'success': True, 'message': f'Task {task_name} executed successfully', 'result': result}), 200
    except Exception as e:
        logging.error(f"Error executing task {task_name}: {str(e)}")
        return jsonify({'success': False, 'error': f'Error executing task {task_name}: {str(e)}'}), 500

@debug_bp.route('/get_available_tasks', methods=['GET'])
@admin_required
def get_available_tasks():
    tasks = [
        'wanted', 'scraping', 'adding', 'checking', 'sleeping', 'unreleased', 'blacklisted',
        'pending_uncached', 'upgrading', 'task_plex_full_scan', 'task_debug_log',
        'task_refresh_release_dates', 'task_purge_not_wanted_magnets_file',
        'task_generate_airtime_report', 'task_check_service_connectivity', 'task_send_notifications',
        'task_check_trakt_early_releases', 'task_reconcile_queues', 'task_check_plex_files',
        'task_update_show_ids', 'task_update_show_titles', 'task_get_plex_watch_history',
        'task_check_database_health', 'task_run_library_maintenance', 'task_update_movie_ids', 'task_update_movie_titles',
        'task_verify_symlinked_files'
    ]
    return jsonify({'tasks': tasks}), 200

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
    from settings import get_setting
    if not get_setting('Debug', 'enable_crash_test', False):
        return jsonify({'success': False, 'error': 'Crash simulation is not enabled'}), 400
        
    # First send the crash notification
    from notifications import send_program_crash_notification
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
        logger.error(f"Error getting verification queue: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        })