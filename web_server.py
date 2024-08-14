from flask import Flask, render_template, jsonify, redirect, url_for, request, session, send_from_directory
from flask_session import Session
import threading
import time
from queue_manager import QueueManager
import logging
import os
from settings import get_all_settings, set_setting, get_setting, load_config, save_config, to_bool
from collections import OrderedDict
from web_scraper import web_scrape, web_scrape_tvshow, process_media_selection, process_torrent_selection, get_available_versions
from debrid.real_debrid import add_to_real_debrid
import re
from datetime import datetime
import sqlite3
from database import get_db_connection
import string

app = Flask(__name__)
app.config['SESSION_TYPE'] = 'filesystem'
Session(app)
app.secret_key = '9683650475'
queue_manager = QueueManager()

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
            if cache_status[result.get('hash')]:
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

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        new_settings = request.json
        current_settings = get_all_settings()
        update_nested_settings(current_settings, new_settings)
        save_config(current_settings)
        return jsonify({"status": "success"})
    else:
        settings = get_all_settings()
        return render_template('settings.html', settings=settings)
    
@app.route('/api/settings', methods=['GET', 'POST'])
def api_settings():
    if request.method == 'POST':
        new_settings = request.json
        current_settings = get_all_settings()
        update_nested_settings(current_settings, new_settings)
        save_config(current_settings)
        return jsonify({"status": "success"})
    else:
        # Return all settings for GET request
        return jsonify(get_all_settings())

def update_nested_settings(current, new):
    for key, value in new.items():
        if key == 'Content Sources':
            if key not in current:
                current[key] = {}
            for source, source_settings in value.items():
                current[key][source] = source_settings
        elif isinstance(value, dict):
            if key not in current or not isinstance(current[key], dict):
                current[key] = {}
            update_nested_settings(current[key], value)
        elif isinstance(value, list):
            if all(isinstance(item, list) and len(item) == 2 for item in value):
                # Handle paired lists
                current[key] = [[str(item[0]), int(item[1])] for item in value]
            else:
                # Handle simple lists
                current[key] = [str(item) for item in value]
        else:
            # Handle boolean and numeric values
            if isinstance(value, str):
                if value.lower() in ('true', 'false'):
                    value = to_bool(value)
                elif value.replace('.', '', 1).isdigit():
                    # Convert to float or int
                    value = float(value) if '.' in value else int(value)
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

def safe_process_queue(queue_name):
    try:
        getattr(queue_manager, f'process_{queue_name.lower()}')()
        update_stats(processed=1)
    except Exception as e:
        logging.error(f"Error processing {queue_name} queue: {str(e)}")
        update_stats(failed=1)

if __name__ == '__main__':
    app.run(debug=True)
