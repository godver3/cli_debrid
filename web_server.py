from flask import Flask, render_template, jsonify, redirect, url_for, request, session, send_from_directory
from flask_session import Session
import threading
import time
from queue_manager import QueueManager
import logging
import os
from settings import get_all_settings, set_setting, get_setting, load_config, save_config, to_bool, ensure_trakt_auth
from collections import OrderedDict
from web_scraper import web_scrape, web_scrape_tvshow, process_media_selection, process_torrent_selection, get_available_versions
from debrid.real_debrid import add_to_real_debrid
import re
from datetime import datetime
import sqlite3
from database import get_db_connection
import string
from settings_web import get_settings_page, update_settings, get_settings
from template_utils import render_settings, render_content_sources
import json
from scraper_manager import ScraperManager
import uuid
from flask import jsonify
from shared import app, update_stats
from run_program import ProgramRunner
from queue_utils import safe_process_queue
from run_program import process_overseerr_webhook, ProgramRunner
from config_manager import add_content_source, delete_content_source, update_content_source, add_scraper
from settings_schema import SETTINGS_SCHEMA
from trakt.core import get_device_code, get_device_token

app = Flask(__name__)
app.config['SESSION_TYPE'] = 'filesystem'
Session(app)
app.secret_key = '9683650475'
queue_manager = QueueManager()
scraper_manager = ScraperManager()

# Disable Werkzeug request logging
log = logging.getLogger('werkzeug')
log.disabled = True

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

# Global variables for statistics
start_time = time.time()
total_processed = 0
successful_additions = 0
failed_additions = 0

CONFIG_FILE = './config/config.json'
TRAKT_CONFIG_PATH = './config/.pytrakt.json'

def get_trakt_config():
    if os.path.exists(TRAKT_CONFIG_PATH):
        with open(TRAKT_CONFIG_PATH, 'r') as f:
            return json.load(f)
    return {}

def save_trakt_config(config):
    with open(TRAKT_CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2)

def update_trakt_config(key, value):
    config = get_trakt_config()
    config[key] = value
    save_trakt_config(config)

def load_config():
    try:
        with open(CONFIG_FILE, 'r') as config_file:
            return json.load(config_file)
    except Exception as e:
        print(f"Error loading config: {str(e)}")
        return {}

def save_config(config):
    try:
        with open(CONFIG_FILE, 'w') as config_file:
            json.dump(config, config_file, indent=2)
        logging.info(f"Config saved successfully: {config}")
    except Exception as e:
        logging.error(f"Error saving config: {str(e)}")

@app.context_processor
def utility_processor():
    return dict(render_settings=render_settings, render_content_sources=render_content_sources)

@app.route('/content_sources/content')
def content_sources_content():
    config = load_config()
    source_types = list(SETTINGS_SCHEMA['Content Sources']['schema'].keys())
    return render_template('settings_tabs/content_sources.html', 
                           settings=config, 
                           source_types=source_types, 
                           settings_schema=SETTINGS_SCHEMA)

@app.route('/content_sources/add', methods=['POST'])
def add_content_source_route():
    logging.info(f"Received request to add content source. Content-Type: {request.content_type}")
    logging.info(f"Request data: {request.data}")
    try:
        if request.is_json:
            source_config = request.json
        elif request.content_type.startswith('multipart/form-data'):
            source_config = request.form.to_dict()
        else:
            return jsonify({'success': False, 'error': f'Unsupported Content-Type: {request.content_type}'}), 415
        
        logging.info(f"Parsed data: {source_config}")
        
        if not source_config:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
        
        source_type = source_config.pop('type', None)
        if not source_type:
            return jsonify({'success': False, 'error': 'No source type provided'}), 400
        
        new_source_id = add_content_source(source_type, source_config)
        return jsonify({'success': True, 'source_id': new_source_id})
    except Exception as e:
        logging.error(f"Error adding content source: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/content_sources/delete', methods=['POST'])
def delete_content_source_route():
    data = request.json
    source_id = data.get('source_id')
    
    if not source_id:
        return jsonify({'success': False, 'error': 'No source ID provided'}), 400

    result = delete_content_source(source_id)
    if result:
        return jsonify({'success': True, 'message': f'Content source {source_id} deleted successfully'})
    else:
        return jsonify({'success': False, 'error': f'Content source {source_id} not found'}), 404

@app.route('/scrapers/add', methods=['POST'])
def add_scraper_route():
    logging.info(f"Received request to add scraper. Content-Type: {request.content_type}")
    logging.info(f"Request data: {request.data}")
    try:
        if request.is_json:
            scraper_config = request.json
        else:
            return jsonify({'success': False, 'error': f'Unsupported Content-Type: {request.content_type}'}), 415
        
        logging.info(f"Parsed data: {scraper_config}")
        
        if not scraper_config:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
        
        scraper_type = scraper_config.pop('type', None)
        if not scraper_type:
            return jsonify({'success': False, 'error': 'No scraper type provided'}), 400
        
        new_scraper_id = add_scraper(scraper_type, scraper_config)
        
        # Log the updated config after adding the scraper
        updated_config = load_config()
        logging.info(f"Updated config after adding scraper: {updated_config}")
        
        return jsonify({'success': True, 'scraper_id': new_scraper_id})
    except Exception as e:
        logging.error(f"Error adding scraper: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/scrapers/content')
def scrapers_content():
    try:
        scrapers = scraper_manager.load_scrapers()
        settings = load_config()
        scraper_types = list(scraper_manager.scraper_settings.keys())
        scraper_settings = scraper_manager.scraper_settings or {}
        return render_template('settings_tabs/scrapers.html', settings=settings, scraper_types=scraper_types, scraper_settings=scraper_settings)
    except Exception as e:
        app.logger.error(f"Error in scrapers_content route: {str(e)}", exc_info=True)
        return jsonify({'error': 'An error occurred while loading scraper settings'}), 500

@app.route('/scrapers/get', methods=['GET'])
def get_scrapers():
    config = load_config()
    scraper_types = scraper_manager.get_scraper_types()
    return render_template('settings_tabs/scrapers.html', settings=config, scraper_types=scraper_types)

@app.route('/scrapers/delete', methods=['POST'])
def delete_scraper():
    data = request.json
    scraper_id = data.get('scraper_id')
    
    if not scraper_id:
        return jsonify({'success': False, 'error': 'No scraper ID provided'}), 400

    config = load_config()
    scrapers = config.get('Scrapers', {})
    
    if scraper_id in scrapers:
        del scrapers[scraper_id]
        config['Scrapers'] = scrapers
        save_config(config)
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'error': 'Scraper not found'}), 404

@app.template_filter('from_json')
def from_json_filter(value):
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}

@app.template_filter('datetime')
def format_datetime(value, format='%Y-%m-%d %H:%M:%S'):
    if value is None:
        return ""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.strftime(format)

@app.route('/')
def index():
    return redirect(url_for('statistics'))

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'),
                               'favicon.ico', mimetype='image/vnd.microsoft.icon')

@app.route('/manifest.json')
def manifest():
    return send_from_directory(os.path.join(app.root_path, 'static'),
                               'site.webmanifest', mimetype='application/json')

@app.route('/database', methods=['GET', 'POST'])
def database():
    conn = get_db_connection()
    cursor = conn.cursor()

    # Get all column names
    cursor.execute("PRAGMA table_info(media_items)")
    all_columns = [column[1] for column in cursor.fetchall()]

    # Get or set selected columns
    if request.method == 'POST':
        selected_columns = request.form.getlist('columns')
        session['selected_columns'] = selected_columns
    else:
        selected_columns = session.get('selected_columns', all_columns[:10])  # Default to first 10 columns

    # Pagination
    page = request.args.get('page', 1, type=int)
    per_page = 100  # Number of items per page

    # Get filter and sort parameters
    filter_column = request.args.get('filter_column', '')
    filter_value = request.args.get('filter_value', '')
    sort_column = request.args.get('sort_column', 'id')  # Default sort by id
    sort_order = request.args.get('sort_order', 'asc')

    # Construct the SQL query
    query = f"SELECT {', '.join(selected_columns)} FROM media_items"
    count_query = "SELECT COUNT(*) FROM media_items"
    params = []

    if filter_column and filter_value:
        where_clause = f" WHERE {filter_column} LIKE ?"
        query += where_clause
        count_query += where_clause
        params.append(f"%{filter_value}%")

    query += f" ORDER BY {sort_column} {sort_order}"
    query += f" LIMIT {per_page} OFFSET {(page - 1) * per_page}"

    # Execute the queries
    cursor.execute(count_query, params)
    total_items = cursor.fetchone()[0]

    cursor.execute(query, params)
    items = [dict(zip(selected_columns, row)) for row in cursor.fetchall()]

    conn.close()

    total_pages = (total_items + per_page - 1) // per_page

    return render_template('database.html', 
                           all_columns=all_columns,
                           selected_columns=selected_columns,
                           items=items,
                           filter_column=filter_column,
                           filter_value=filter_value,
                           sort_column=sort_column,
                           sort_order=sort_order,
                           page=page,
                           total_pages=total_pages)

@app.route('/add_to_real_debrid', methods=['POST'])
def add_torrent_to_real_debrid():
    try:
        magnet_link = request.form.get('magnet_link')
        if not magnet_link:
            return jsonify({'error': 'No magnet link provided'}), 400

        result = add_to_real_debrid(magnet_link)
        if result:
            if result == 'downloading' or result == 'queued':
                return jsonify({'message': 'Uncached torrent added to Real-Debrid successfully'})
            else:
                return jsonify({'message': 'Cached torrent added to Real-Debrid successfully'})
        else:
            return jsonify({'error': 'Failed to add torrent to Real-Debrid'}), 500

    except Exception as e:
        logging.error(f"Error adding torrent to Real-Debrid: {str(e)}")
        return jsonify({'error': 'An error occurred while adding to Real-Debrid'}), 500

@app.route('/statistics')
def statistics():
    uptime = time.time() - start_time
    stats = {
        'total_processed': total_processed,
        'successful_additions': successful_additions,
        'failed_additions': failed_additions,
        'uptime': uptime
    }
    return render_template('statistics.html', stats=stats)

@app.route('/scraper', methods=['GET', 'POST'])
def scraper():
    versions = get_available_versions()
    if request.method == 'POST':
        search_term = request.form.get('search_term')
        version = request.form.get('version')
        if search_term:
            session['search_term'] = search_term  # Store the search term in the session
            session['version'] = version  # Store the version in the session
            results = web_scrape(search_term, version)
            return jsonify(results)
        else:
            return jsonify({'error': 'No search term provided'})
    
    return render_template('scraper.html', versions=versions)

@app.route('/select_season', methods=['GET', 'POST'])
def select_season():
    versions = get_available_versions()
    if request.method == 'POST':
        media_id = request.form.get('media_id')
        title = request.form.get('title')
        year = request.form.get('year')
        if media_id:
            results = web_scrape_tvshow(media_id, title, year)
            return jsonify(results)
        else:
            return jsonify({'error': 'No media_id provided'})
    
    return render_template('scraper.html', versions=versions)

@app.route('/select_episode', methods=['GET', 'POST'])
def select_episode():
    versions = get_available_versions()
    if request.method == 'POST':
        media_id = request.form.get('media_id')
        season = request.form.get('season')
        title = request.form.get('title')
        year = request.form.get('year')
        if media_id:
            results = web_scrape_tvshow(media_id, title, year, season)
            return jsonify(results)
        else:
            return jsonify({'error': 'No media_id provided'})
    
    return render_template('scraper.html', versions=versions)

@app.route('/select_media', methods=['POST'])
def select_media():
    try:
        media_id = request.form.get('media_id')
        title = request.form.get('title')
        year = request.form.get('year')
        media_type = request.form.get('media_type')
        season = request.form.get('season')
        episode = request.form.get('episode')
        multi = request.form.get('multi', 'false').lower() in ['true', '1', 'yes', 'on']
        version = request.form.get('version')

        if not version or version == 'undefined':
            version = get_setting('Scraping', 'default_version', '1080p')  # Fallback to a default version

        season = int(season) if season and season.isdigit() else None
        episode = int(episode) if episode and episode.isdigit() else None

        # Adjust multi and episode based on season
        if media_type == 'tv' and season is not None:
            if episode is None:
                episode = 1
                multi = True
            else:
                multi = False

        logging.info(f"Selecting media: {media_id}, {title}, {year}, {media_type}, S{season or 'None'}E{episode or 'None'}, multi={multi}, version={version}")

        torrent_results, cache_status = process_media_selection(media_id, title, year, media_type, season, episode, multi, version)
        cached_results = []
        for result in torrent_results:
            if cache_status.get(result.get('hash'), False):
                result['cached'] = 'RD'
            else:
                result['cached'] = ''
            cached_results.append(result)

        return jsonify({'torrent_results': cached_results})
    except Exception as e:
        logging.error(f"Error in select_media: {str(e)}")
        return jsonify({'error': 'An error occurred while selecting media'}), 500



@app.route('/add_torrent', methods=['POST'])
def add_torrent():
    torrent_index = int(request.form.get('torrent_index'))
    torrent_results = session.get('torrent_results', [])
    
    if 0 <= torrent_index < len(torrent_results):
        result = process_torrent_selection(torrent_index, torrent_results)
        if result['success']:
            return render_template('scraper.html', success_message=result['message'])
        else:
            return render_template('scraper.html', error=result['error'])
    else:
        return render_template('scraper.html', error="Invalid torrent selection")


@app.route('/logs')
def logs():
    logs = get_recent_logs(100)  # Get the last 100 log entries
    return render_template('logs.html', logs=logs)

@app.route('/settings', methods=['GET'])
def settings():
    try:
        config = load_config()
        scraper_types = list(scraper_manager.scraper_settings.keys())
        source_types = list(SETTINGS_SCHEMA['Content Sources']['schema'].keys())
        
        # Ensure 'Scrapers' exists in the config
        if 'Scrapers' not in config:
            config['Scrapers'] = {}
        
        # Only keep the scrapers that are actually configured
        configured_scrapers = {}
        for scraper, scraper_config in config['Scrapers'].items():
            scraper_type = scraper.split('_')[0]  # Assuming format like 'Zilean_1'
            if scraper_type in scraper_manager.scraper_settings:
                configured_scrapers[scraper] = scraper_config
        
        config['Scrapers'] = configured_scrapers
        
        # Ensure 'Content Sources' exists in the config
        if 'Content Sources' not in config:
            config['Content Sources'] = {}
        
        # Ensure each content source is a dictionary
        for source, source_config in config['Content Sources'].items():
            if not isinstance(source_config, dict):
                config['Content Sources'][source] = {}
        
        return render_template('settings_base.html', 
                               settings=config, 
                               scraper_types=scraper_types, 
                               scraper_settings=scraper_manager.scraper_settings,
                               source_types=source_types,
                               content_source_settings=SETTINGS_SCHEMA['Content Sources']['schema'],
                               settings_schema=SETTINGS_SCHEMA)
    except Exception as e:
        app.logger.error(f"Error in settings route: {str(e)}", exc_info=True)
        return render_template('error.html', error_message="An error occurred while loading settings."), 500

@app.route('/scraping/get')
def get_scraping_settings():
    config = load_config()
    scraping_settings = config.get('Scraping', {})
    return jsonify(scraping_settings)

@app.route('/api/settings', methods=['GET', 'POST'])
def api_settings():
    if request.method == 'POST':
        new_settings = request.json
        current_settings = load_config()
        update_nested_settings(current_settings, new_settings)
        save_config(current_settings)
        return jsonify({"status": "success"})
    else:
        return jsonify(load_config())

def update_nested_settings(current, new):
    for key, value in new.items():
        if isinstance(value, dict):
            if key not in current or not isinstance(current[key], dict):
                current[key] = {}
            if key == 'Content Sources':
                for source_id, source_config in value.items():
                    if source_id in current[key]:
                        update_content_source(source_id, source_config)
                    else:
                        add_content_source(source_config['type'], source_config)
            else:
                update_nested_settings(current[key], value)
        else:
            current[key] = value

@app.route('/queues')
def queues():
    queue_contents = queue_manager.get_queue_contents()
    for queue_name, items in queue_contents.items():
        if queue_name == 'Upgrading':
            for item in items:
                item['time_added'] = item.get('time_added', datetime.now())
                item['upgrades_found'] = item.get('upgrades_found', 0)
        elif queue_name == 'Checking':
            for item in items:
                item['time_added'] = item.get('time_added', datetime.now())
        elif queue_name == 'Sleeping':
            for item in items:
                item['wake_count'] = queue_manager.get_wake_count(item['id'])  # Use the new get_wake_count method

    upgrading_queue = queue_contents.get('Upgrading', [])
    logging.info(f"Rendering queues page. UpgradingQueue size: {len(upgrading_queue)}")
    return render_template('queues.html', queue_contents=queue_contents, upgrading_queue=upgrading_queue)

@app.route('/api/queue_contents')
def api_queue_contents():
    contents = queue_manager.get_queue_contents()
    # Ensure wake counts are included for Sleeping queue items
    if 'Sleeping' in contents:
        for item in contents['Sleeping']:
            item['wake_count'] = queue_manager.get_wake_count(item['id'])
    return jsonify(contents)

@app.route('/api/logs')
def api_logs():
    logs = get_recent_logs(100)
    return jsonify(logs)

def get_recent_logs(n):
    log_path = 'logs/info.log'
    if not os.path.exists(log_path):
        return []
    with open(log_path, 'r') as f:
        logs = f.readlines()
    return logs[-n:]

def run_server():
    app.run(debug=True, use_reloader=False, host='0.0.0.0')

def start_server():
    server_thread = threading.Thread(target=run_server)
    server_thread.daemon = True
    server_thread.start()

# Function to update statistics
def update_stats(processed=0, successful=0, failed=0):
    global total_processed, successful_additions, failed_additions
    total_processed += processed
    successful_additions += successful
    failed_additions += failed
'''
def safe_process_queue(queue_name):
    try:
        getattr(queue_manager, f'process_{queue_name.lower()}')()
        update_stats(processed=1)
    except Exception as e:
        logging.error(f"Error processing {queue_name} queue: {str(e)}")
        update_stats(failed=1)
'''
program_runner = None

@app.route('/api/start_program', methods=['POST'])
def start_program():
    global program_runner
    if program_runner is None or not program_runner.is_running():
        program_runner = ProgramRunner()
        # Start the program runner in a separate thread to avoid blocking the Flask server
        threading.Thread(target=program_runner.start).start()
        return jsonify({"status": "success", "message": "Program started"})
    else:
        return jsonify({"status": "error", "message": "Program is already running"})

@app.route('/api/reset_program', methods=['POST'])
def reset_program():
    global program_runner
    if program_runner is not None:
        program_runner.stop()
    program_runner = None
    return jsonify({"status": "success", "message": "Program reset"})
    
@app.route('/api/program_status', methods=['GET'])
def program_status():
    global program_runner
    status = "Running" if program_runner is not None and program_runner.is_running() else "Initialized"
    return jsonify({"status": status})

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    logging.debug(f"Received webhook: {data}")
    try:
        process_overseerr_webhook(data)
        return jsonify({"status": "success"}), 200
    except Exception as e:
        logging.error(f"Error processing webhook: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/versions/add', methods=['POST'])
def add_version():
    version_name = request.form.get('name')
    if not version_name:
        return jsonify({'success': False, 'error': 'No version name provided'}), 400

    config = load_config()
    if 'Scraping' not in config:
        config['Scraping'] = {}
    if 'versions' not in config['Scraping']:
        config['Scraping']['versions'] = {}

    if version_name in config['Scraping']['versions']:
        return jsonify({'success': False, 'error': 'Version already exists'}), 400

    # Add the new version with default settings
    config['Scraping']['versions'][version_name] = {
        'enable_hdr': False,
        'max_resolution': '1080p',
        'resolution_wanted': '<=',
        'resolution_weight': 3,
        'hdr_weight': 3,
        'similarity_weight': 3,
        'size_weight': 3,
        'bitrate_weight': 3,
        'preferred_filter_in': [],
        'preferred_filter_out': [],
        'filter_in': [],
        'filter_out': [],
        'min_size_gb': 0.01
    }

    save_config(config)
    return jsonify({'success': True})

@app.route('/versions/delete', methods=['POST'])
def delete_version():
    data = request.json
    version_id = data.get('version_id')
    
    if not version_id:
        return jsonify({'success': False, 'error': 'No version ID provided'}), 400

    config = load_config()
    if 'Scraping' in config and 'versions' in config['Scraping'] and version_id in config['Scraping']['versions']:
        del config['Scraping']['versions'][version_id]
        save_config(config)
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'error': 'Version not found'}), 404

@app.route('/scraping/content')
def scraping_content():
    config = load_config()
    return render_template('settings_tabs/scraping.html', settings=config, settings_schema=SETTINGS_SCHEMA)

@app.route('/trakt_auth', methods=['POST'])
def trakt_auth():
    try:
        client_id = get_setting('Trakt', 'client_id')
        client_secret = get_setting('Trakt', 'client_secret')
        
        if not client_id or not client_secret:
            return jsonify({'error': 'Trakt client ID or secret not set. Please configure in settings.'}), 400
        
        device_code_response = get_device_code(client_id, client_secret)
        
        # Store the device code response in the Trakt config file
        update_trakt_config('device_code_response', device_code_response)
        
        return jsonify({
            'user_code': device_code_response['user_code'],
            'verification_url': device_code_response['verification_url'],
            'device_code': device_code_response['device_code']
        })
    except Exception as e:
        app.logger.error(f"Error in Trakt authorization: {str(e)}", exc_info=True)
        return jsonify({'error': 'Unable to start authorization process'}), 500

# Update the existing trakt_auth_status route
@app.route('/trakt_auth_status', methods=['POST'])
def trakt_auth_status():
    try:
        trakt_config = get_trakt_config()
        device_code_response = trakt_config.get('device_code_response')
        
        if not device_code_response:
            return jsonify({'error': 'No pending Trakt authorization'}), 400
        
        client_id = get_setting('Trakt', 'client_id')
        client_secret = get_setting('Trakt', 'client_secret')
        device_code = device_code_response['device_code']
        
        response = get_device_token(device_code, client_id, client_secret)
        
        if response.status_code == 200:
            token_data = response.json()
            
            # Store the new tokens
            update_trakt_config('CLIENT_ID', client_id)
            update_trakt_config('CLIENT_SECRET', client_secret)
            update_trakt_config('OAUTH_TOKEN', token_data['access_token'])
            update_trakt_config('OAUTH_REFRESH', token_data['refresh_token'])
            update_trakt_config('OAUTH_EXPIRES_AT', int(time.time()) + token_data['expires_in'])
            
            # Remove the device code response as it's no longer needed
            trakt_config = get_trakt_config()
            trakt_config.pop('device_code_response', None)
            save_trakt_config(trakt_config)
            
            return jsonify({'status': 'authorized'})
        elif response.status_code == 400:
            return jsonify({'status': 'pending'})
        else:
            return jsonify({'status': 'error', 'message': response.text}), response.status_code
    except Exception as e:
        app.logger.error(f"Error checking Trakt authorization status: {str(e)}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500

# Add a new route to check if Trakt is already authorized
@app.route('/trakt_auth_status', methods=['GET'])
def check_trakt_auth_status():
    trakt_config = get_trakt_config()
    if 'OAUTH_TOKEN' in trakt_config and 'OAUTH_EXPIRES_AT' in trakt_config:
        if trakt_config['OAUTH_EXPIRES_AT'] > time.time():
            return jsonify({'status': 'authorized'})
    return jsonify({'status': 'unauthorized'})

if __name__ == '__main__':
    app.run(debug=True)
