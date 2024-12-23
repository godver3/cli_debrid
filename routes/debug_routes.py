from flask import jsonify, Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask.json import jsonify
from initialization import get_all_wanted_from_enabled_sources
from run_program import get_and_add_recent_collected_from_plex, get_and_add_all_collected_from_plex, ProgramRunner
from manual_blacklist import add_to_manual_blacklist, remove_from_manual_blacklist, get_manual_blacklist
from settings import get_all_settings, get_setting, set_setting
import logging
from routes import admin_required
from content_checkers.overseerr import get_wanted_from_overseerr
from content_checkers.collected import get_wanted_from_collected
from content_checkers.trakt import get_wanted_from_trakt_lists, get_wanted_from_trakt_watchlist
from content_checkers.mdb_list import get_wanted_from_mdblists
from metadata.metadata import process_metadata
from database import add_wanted_items, get_db_connection, bulk_delete_by_id, create_tables, verify_database
import os
from api_tracker import api 
import time
from metadata.metadata import get_metadata, get_tmdb_id_and_media_type, refresh_release_dates
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

debug_bp = Blueprint('debug', __name__)

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
            get_and_add_all_collected_from_plex()
            message = 'Successfully retrieved and added all collected items from Plex'
        elif collection_type == 'recent':
            get_and_add_recent_collected_from_plex()
            message = 'Successfully retrieved and added recent collected items from Plex'
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
    confirm_delete = request.form.get('confirm_delete')
    if confirm_delete != 'DELETE':
        return jsonify({'success': False, 'error': 'Invalid confirmation'})

    conn = get_db_connection()
 
    try:
        # Close any open database connections
        conn.close()

        # Get db_content directory from environment variable with fallback
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        
        # Delete the media_items.db file
        db_path = os.path.join(db_content_dir, 'media_items.db')
        if os.path.exists(db_path):
            os.remove(db_path)
            logging.info(f"Deleted media_items.db file: {db_path}")
        else:
            logging.info(f"media_items.db file not found: {db_path}")

        # Recreate the tables
        create_tables()
        verify_database()

        # Delete the trakt cache file
        from content_checkers.trakt import CACHE_FILE
        if os.path.exists(CACHE_FILE):
            os.remove(CACHE_FILE)
            logging.info(f"Deleted trakt cache file: {CACHE_FILE}")
            
        return jsonify({'success': True, 'message': 'Database deleted and tables recreated successfully'})
    except Exception as e:
        logging.error(f"Error deleting database: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': f'An error occurred: {str(e)}'})

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

        if action == 'add':
            details = None
            media_type = None
            tmdb_id = None

            # Try to get TMDB ID and media type
            try:
                tmdb_id, media_type = get_tmdb_id_and_media_type(imdb_id)
                logging.info(f"Retrieved TMDB ID: {tmdb_id}, Media Type: {media_type}")
            except Exception as e:
                logging.error(f"Error in get_tmdb_id_and_media_type: {str(e)}")

            # If we have media_type, try to fetch metadata
            if media_type:
                try:
                    details = get_metadata(imdb_id=imdb_id, tmdb_id=tmdb_id, item_media_type=media_type)
                except Exception as e:
                    logging.error(f"Error fetching metadata for {imdb_id}: {str(e)}")

            # If we still don't have details, try a basic IMDB lookup
            if not details:
                try:
                    from imdb import IMDb
                    ia = IMDb()
                    movie = ia.get_movie(imdb_id[2:])  # Remove 'tt' prefix
                    details = {
                        'title': movie.get('title'),
                        'year': movie.get('year'),
                    }
                    logging.info(f"Retrieved basic details from IMDb: {details}")
                except Exception as e:
                    logging.error(f"Error fetching basic details from IMDb: {str(e)}")

            # If we still don't have details, log an error and flash a message
            if not details:
                error_msg = f"Could not fetch details for IMDb ID: {imdb_id}"
                logging.error(error_msg)
                flash(error_msg, 'error')
                return redirect(url_for('debug.manual_blacklist'))

            # Process the details and add to blacklist
            title = details.get('title')
            year = details.get('year')
                        
            # After retrieving tmdb_id and media_type
            if media_type is None or media_type == 'None' or media_type.lower() == 'none':
                media_type = 'TV Show'
                logging.info(f"Media type was None or 'None', setting to 'TV Show' for IMDb ID: {imdb_id}")

            # Then proceed with fetching metadata and adding to blacklist
            if media_type:
                try:
                    details = get_metadata(imdb_id=imdb_id, tmdb_id=tmdb_id, item_media_type=media_type)
                except Exception as e:
                    logging.error(f"Error fetching metadata for {imdb_id}: {str(e)}")
                    details = None

            if title and year:
                add_to_manual_blacklist(imdb_id, media_type, title, str(year))
                flash(f'Added {imdb_id}: {title} ({year}) to manual blacklist as {media_type or "unknown"}', 'success')
            else:
                flash(f'Incomplete details for {imdb_id}. Title: {title}, Year: {year}', 'error')
        
        elif action == 'remove':
            remove_from_manual_blacklist(imdb_id)
            flash(f'Removed {imdb_id} from manual blacklist', 'success')

    blacklist = get_manual_blacklist()
    return render_template('manual_blacklist.html', blacklist=blacklist)

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

    set_setting('Trakt', 'user_watchlist_enabled', trakt_watchlist_enabled)
    set_setting('Trakt', 'trakt_lists', trakt_lists)

def get_and_add_wanted_content(source_id):
    content_sources = get_all_settings().get('Content Sources', {})
    source_data = content_sources[source_id]
    source_type = source_id.split('_')[0]
    versions = source_data.get('versions', {})

    logging.info(f"Processing source: {source_id}")
    
    wanted_content = []
    if source_type == 'Overseerr':
        wanted_content = get_wanted_from_overseerr()
    elif source_type == 'MDBList':
        mdblist_urls = source_data.get('urls', '').split(',')
        for mdblist_url in mdblist_urls:
            mdblist_url = mdblist_url.strip()
            wanted_content.extend(get_wanted_from_mdblists(mdblist_url, versions))
    elif source_type == 'Trakt Watchlist':
        update_trakt_settings(content_sources)
        wanted_content = get_wanted_from_trakt_watchlist()
    elif source_type == 'Trakt Lists':
        update_trakt_settings(content_sources)
        trakt_lists = source_data.get('trakt_lists', '').split(',')
        for trakt_list in trakt_lists:
            trakt_list = trakt_list.strip()
            wanted_content.extend(get_wanted_from_trakt_lists(trakt_list, versions))
    elif source_type == 'Collected':
        wanted_content = get_wanted_from_collected()

    logging.debug(f"wanted_content for {source_id}: {wanted_content}")

    if wanted_content:
        total_items = 0
        for items, item_versions in wanted_content:
            logging.debug(f"Processing items: {len(items)}, versions: {item_versions}")
            processed_items = process_metadata(items)
            if processed_items:
                all_items = processed_items.get('movies', []) + processed_items.get('episodes', [])
                logging.debug(f"Calling add_wanted_items with {len(all_items)} items and versions: {item_versions}")
                add_wanted_items(all_items, item_versions)
                total_items += len(all_items)
        
        logging.info(f"Added {total_items} wanted items from {source_id}")
    else:
        logging.warning(f"No wanted content retrieved from {source_id}")

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
                version = TRIM(version, '*')
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
        test_notifications = [
            {
                'type': 'movie',
                'title': 'Test Movie 1',
                'year': 2023,
                'tmdb_id': '123456',
                'original_collected_at': now.isoformat(),
                'version': 'Default',
                'is_upgrade': False
            },
            {
                'type': 'movie',
                'title': 'Test Movie 2',
                'year': 2023,
                'tmdb_id': '234567',
                'original_collected_at': (now + timedelta(hours=1)).isoformat(),
                'version': 'Extended',
                'is_upgrade': True
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
                'is_upgrade': False
            }
        ]

        # Fetch enabled notifications
        enabled_notifications = get_all_settings().get('Notifications', {})
        
        # Send test notifications
        send_notifications(test_notifications, enabled_notifications)
        
        return jsonify({'success': True, 'message': 'Test notification sent successfully'}), 200

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
        'task_check_trakt_early_releases', 'task_reconcile_queues'
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