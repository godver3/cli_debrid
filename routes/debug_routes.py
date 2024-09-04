from flask import jsonify, Blueprint, render_template, request, redirect, url_for, flash
from flask.json import jsonify
from flask_login import login_required, current_user

from utilities.debug_commands import get_and_add_all_collected_from_plex, get_and_add_recent_collected_from_plex, get_and_add_wanted_content, get_all_wanted_from_enabled_sources
from metadata.metadata import get_overseerr_show_details, get_overseerr_cookies, get_overseerr_movie_details, get_tmdb_id_and_media_type
from manual_blacklist import add_to_manual_blacklist, remove_from_manual_blacklist, get_manual_blacklist
from database import get_db_connection
from settings import get_all_settings, get_setting
import logging
from routes import admin_required

debug_bp = Blueprint('debug', __name__)

@debug_bp.route('/debug_functions')
def debug_functions():
    content_sources = get_all_settings().get('Content Sources', {})
    enabled_sources = {source: data for source, data in content_sources.items() if data.get('enabled', False)}
    return render_template('debug_functions.html', content_sources=enabled_sources)

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
            WHERE state IN ('Adding', 'Blacklisted', 'Checking', 'Scraping', 'Sleeping', 'Unreleased', 'Wanted')
        ''')
        items = cursor.fetchall()

        queue_contents = {
            'Adding': [], 'Blacklisted': [], 'Checking': [], 'Scraping': [],
            'Sleeping': [], 'Unreleased': [], 'Wanted': []
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
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    
    if not overseerr_url or not overseerr_api_key:
        flash('Overseerr URL or API key not set. Please configure in settings.', 'error')
        return redirect(url_for('settings'))

    cookies = get_overseerr_cookies(overseerr_url)

    if request.method == 'POST':
        action = request.form.get('action')
        imdb_id = request.form.get('imdb_id')

        if action == 'add':
            tmdb_id, media_type = get_tmdb_id_and_media_type(overseerr_url, overseerr_api_key, imdb_id)
            
            if tmdb_id and media_type:
                # Fetch details based on media type
                if media_type == 'movie':
                    details = get_overseerr_movie_details(overseerr_url, overseerr_api_key, tmdb_id, cookies)
                else:  # TV show
                    details = get_overseerr_show_details(overseerr_url, overseerr_api_key, tmdb_id, cookies)
                
                if details:
                    title = details.get('title') if media_type == 'movie' else details.get('name')
                    year = details.get('releaseDate', '')[:4] if media_type == 'movie' else details.get('firstAirDate', '')[:4]
                    
                    add_to_manual_blacklist(imdb_id, media_type, title, year)
                    flash(f'Added {imdb_id}: {title} ({year}) to manual blacklist as {media_type}', 'success')
                else:
                    flash(f'Could not fetch details for {imdb_id}', 'error')
            else:
                flash(f'Could not determine TMDB ID and media type for IMDb ID {imdb_id}', 'error')
        
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
    tmdb_id, media_type = get_tmdb_id_and_media_type(overseerr_url, overseerr_api_key, imdb_id)
    
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
    collection_type = request.form.get('collection_type')
    
    try:
        if collection_type == 'all':
            get_and_add_all_collected_from_plex()
        elif collection_type == 'recent':
            get_and_add_recent_collected_from_plex()
        else:
            return jsonify({'success': False, 'error': 'Invalid collection type'}), 400

        return jsonify({'success': True, 'message': f'Successfully retrieved and added {collection_type} collected items from Plex'}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@debug_bp.route('/api/get_wanted_content', methods=['POST'])
def get_wanted_content():
    source = request.form.get('source')
    
    try:
        if source == 'all':
            get_all_wanted_from_enabled_sources()
            message = 'Successfully retrieved and added wanted items from all enabled sources'
        else:
            get_and_add_wanted_content(source)
            message = f'Successfully retrieved and added wanted items from {source}'

        return jsonify({'success': True, 'message': message}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500