from flask import jsonify, Blueprint, render_template, request, redirect, url_for, flash, current_app
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
from content_checkers.trakt import get_wanted_from_trakt_lists, get_wanted_from_trakt_watchlist, get_wanted_from_trakt_collection
from content_checkers.mdb_list import get_wanted_from_mdblists
from content_checkers.plex_rss_watchlist import get_wanted_from_plex_rss, get_wanted_from_friends_plex_rss
from metadata.metadata import process_metadata, get_metadata
from cli_battery.app.direct_api import DirectAPI
from database import add_wanted_items, get_db_connection, bulk_delete_by_id, create_tables, verify_database
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
        
        # Recreate tables
        create_tables()
        
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
    sorted_blacklist = dict(sorted(blacklist.items(), key=lambda x: x[1]['title'].lower()))
    
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

    logging.info(f"Processing source: {source_id}")
    
    wanted_content = []
    if source_type == 'Overseerr':
        wanted_content = get_wanted_from_overseerr()
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
        wanted_content = get_wanted_from_trakt_watchlist()
    elif source_type == 'Trakt Lists':
        update_trakt_settings(content_sources)
        trakt_lists = source_data.get('trakt_lists', '').split(',')
        for trakt_list in trakt_lists:
            trakt_list = trakt_list.strip()
            wanted_content.extend(get_wanted_from_trakt_lists(trakt_list, versions))
    elif source_type == 'Trakt Collection':
        update_trakt_settings(content_sources)
        wanted_content = get_wanted_from_trakt_collection()
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
                # Add content source
                for item in all_items:
                    item['content_source'] = source_id
                logging.debug(f"Calling add_wanted_items with {len(all_items)} items and versions: {item_versions}")
                add_wanted_items(all_items, item_versions)
                total_items += len(all_items)
        
        logging.info(f"Added {total_items} wanted items from {source_id}")
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
                'new_state': 'Collected'  # Adding new_state for NEW indicator
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
                'new_state': 'Collected'  # Adding new_state for upgrade indicator
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
                'new_state': 'Collected'
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
                'new_state': 'Collected'  # This indicates it's a completed upgrade
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
                'media_type': 'movie'
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
                'media_type': 'movie'
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
                'media_type': 'tv'
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
        'task_get_plex_watch_history': program_runner.task_get_plex_watch_history,
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
        'task_update_show_ids', 'task_update_show_titles', 'task_get_plex_watch_history'
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
        # Get database connection
        conn = get_db_connection()
        cursor = conn.cursor()

        # Get symlinked files path from settings
        symlinked_path = get_setting('File Management', 'symlinked_files_path', '/mnt/symlinked')
        if not symlinked_path:
            logging.error("Symlinked files path not configured")
            return "Symlinked files path not configured", 400

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
            logging.error("No items found with location_on_disk set")
            return "No items found with location_on_disk set", 404

        logging.info(f"Found {len(items)} items to convert to symlinks")

        # Convert items to symlinks
        from utilities.local_library_scan import convert_item_to_symlink
        results = []
        processed = 0
        total = len(items)
        wanted_count = 0
        deleted_count = 0
        skipped_count = 0

        for item in items:
            item_dict = dict(item)
            
            # Check if item is already in symlink folder
            current_location = item_dict.get('location_on_disk', '')
            if current_location.startswith(symlinked_path):
                logging.info(f"Skipping {item_dict['title']} ({item_dict['version']}) - already in symlink folder")
                skipped_count += 1
                continue

            result = convert_item_to_symlink(item_dict)
            results.append(result)

            if result['success']:
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
                    # Check if we have another item with same title, type, and version that's either Wanted or Collected
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
                        logging.info(f"Deleted item {item_dict['title']} ({item_dict['version']}) as duplicate exists")
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
                        logging.info(f"Moved item {item_dict['title']} ({item_dict['version']}) to Wanted state")
            
            # Log progress every 50 items
            processed += 1
            if processed % 50 == 0:
                logging.info(f"Progress: {processed}/{total} items processed")
                conn.commit()  # Intermediate commit to avoid long transactions

        conn.commit()
        conn.close()

        # Count successes and failures
        successes = sum(1 for r in results if r['success'])
        failures = sum(1 for r in results if not r['success'])

        # Group failures by error type for better reporting
        error_summary = {}
        for r in results:
            if not r['success']:
                error_type = r['error']
                if error_type not in error_summary:
                    error_summary[error_type] = 0
                error_summary[error_type] += 1

        # Log the summary
        logging.info(f"Processed {len(results)} items: {successes} successful, {failures} failed")
        if failures > 0:
            logging.warning("Error summary:")
            for error_type, count in error_summary.items():
                logging.warning(f"- {error_type}: {count} items")

        summary = f"Processed {len(results)} items: {successes} successful, {failures} failed"
        if wanted_count > 0 or deleted_count > 0 or skipped_count > 0:
            if wanted_count > 0:
                summary += f"\nMoved {wanted_count} items back to Wanted state"
            if deleted_count > 0:
                summary += f"\nDeleted {deleted_count} duplicate items"
            if skipped_count > 0:
                summary += f"\nSkipped {skipped_count} items already in symlink folder"

        return summary, 200

    except Exception as e:
        logging.error(f"Error converting library to symlinks: {str(e)}")
        return f"Error converting library to symlinks: {str(e)}", 500

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