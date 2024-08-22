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
from datetime import datetime, timedelta
import sqlite3
from database import get_db_connection, get_collected_counts, remove_from_media_items
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
from config_manager import add_content_source, delete_content_source, update_content_source, add_scraper, load_config, save_config, get_version_settings, update_all_content_sources
from settings_schema import SETTINGS_SCHEMA
from trakt.core import get_device_code, get_device_token
from scraper.scraper import scrape
from utilities.manual_scrape import search_overseerr, get_details
from settings import get_all_settings
import string
from itertools import groupby
from operator import itemgetter
from flask import current_app
import requests

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

def get_airing_soon():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    today = datetime.now().date()
    tomorrow = today + timedelta(days=1)
    
    query = """
    SELECT title, release_date, airtime
    FROM media_items
    WHERE type = 'episode' AND release_date BETWEEN ? AND ?
    ORDER BY release_date, airtime
    """
    
    cursor.execute(query, (today.isoformat(), tomorrow.isoformat()))
    results = cursor.fetchall()
    
    conn.close()
    
    # Group by title and take the earliest air date/time for each show
    grouped_results = []
    for key, group in groupby(results, key=itemgetter(0)):
        group_list = list(group)
        grouped_results.append({
            'title': key,
            'air_date': group_list[0][1],
            'air_time': group_list[0][2]
        })
    
    return grouped_results

def get_upcoming_releases():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    today = datetime.now().date()
    next_week = today + timedelta(days=7)
    
    query = """
    SELECT DISTINCT title, release_date
    FROM media_items
    WHERE type = 'movie' AND release_date BETWEEN ? AND ?
    ORDER BY release_date, title
    """
    
    cursor.execute(query, (today.isoformat(), next_week.isoformat()))
    results = cursor.fetchall()
    
    conn.close()
    
    # Group by release date
    grouped_results = {}
    for title, release_date in results:
        if release_date not in grouped_results:
            grouped_results[release_date] = set()
        grouped_results[release_date].add(title)
    
    # Format the results
    formatted_results = [
        {'titles': list(titles), 'release_date': date}
        for date, titles in grouped_results.items()
    ]
    
    return formatted_results

def get_recently_aired_and_airing_soon():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    now = datetime.now()
    two_days_ago = now - timedelta(days=2)
    
    query = """
    SELECT DISTINCT title, season_number, episode_number, release_date, airtime
    FROM media_items
    WHERE type = 'episode' AND release_date >= ? AND release_date <= ?
    ORDER BY release_date, airtime
    """
    
    cursor.execute(query, (two_days_ago.date().isoformat(), (now + timedelta(days=1)).date().isoformat()))
    results = cursor.fetchall()
    
    conn.close()
    
    recently_aired = []
    airing_soon = []
    
    for result in results:
        title, season, episode, release_date, airtime = result
        air_datetime = datetime.combine(datetime.fromisoformat(release_date), datetime.strptime(airtime, '%H:%M').time())
        
        item = {
            'title': title,
            'season': season,
            'episode': episode,
            'air_datetime': air_datetime
        }
        
        if air_datetime <= now:
            recently_aired.append(item)
        else:
            airing_soon.append(item)
    
    return recently_aired, airing_soon

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

@app.context_processor
def utility_processor():
    return dict(render_settings=render_settings, render_content_sources=render_content_sources)

@app.route('/delete_item', methods=['POST'])
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
    try:
        if request.is_json:
            source_config = request.json
        else:
            return jsonify({'success': False, 'error': f'Unsupported Content-Type: {request.content_type}'}), 415
        
        source_type = source_config.pop('type', None)
        if not source_type:
            return jsonify({'success': False, 'error': 'No source type provided'}), 400
        
        # Ensure versions is a list
        if 'versions' in source_config:
            if isinstance(source_config['versions'], bool):
                source_config['versions'] = []
            elif isinstance(source_config['versions'], str):
                source_config['versions'] = [source_config['versions']]
        
        new_source_id = add_content_source(source_type, source_config)
        
        return jsonify({'success': True, 'source_id': new_source_id})
    except Exception as e:
        logging.error(f"Error adding content source: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/content_sources/delete', methods=['POST'])
def delete_content_source_route():
    source_id = request.json.get('source_id')
    if not source_id:
        return jsonify({'success': False, 'error': 'No source ID provided'}), 400

    logging.info(f"Attempting to delete content source: {source_id}")
    
    success = delete_content_source(source_id)
    
    if success:
        # Update the config in web_server.py
        config = load_config()
        if 'Content Sources' in config and source_id in config['Content Sources']:
            del config['Content Sources'][source_id]
            save_config(config)
        
        logging.info(f"Content source {source_id} deleted successfully")
        return jsonify({'success': True})
    else:
        logging.warning(f"Failed to delete content source: {source_id}")
        return jsonify({'success': False, 'error': 'Source not found or already deleted'}), 404

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

    # Get filter and sort parameters
    filter_column = request.args.get('filter_column', '')
    filter_value = request.args.get('filter_value', '')
    sort_column = request.args.get('sort_column', 'id')  # Default sort by id
    sort_order = request.args.get('sort_order', 'asc')
    content_type = request.args.get('content_type', 'movie')  # Default to 'movie'
    current_letter = request.args.get('letter', 'A')

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

    if where_clauses:
        query += " WHERE " + " AND ".join(where_clauses)

    query += f" ORDER BY {sort_column} {sort_order}"

    # Execute the query
    cursor.execute(query, params)
    items = cursor.fetchall()

    conn.close()

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

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({
            'table': render_template('database_table.html', 
                                     items=items, 
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
    uptime = int(time.time() - start_time)
    collected_counts = get_collected_counts()
    recently_aired, airing_soon = get_recently_aired_and_airing_soon()
    upcoming_releases = get_upcoming_releases()
    now = datetime.now()
    stats = {
        'uptime': uptime,
        'total_movies': collected_counts['total_movies'],
        'total_shows': collected_counts['total_shows'],
        'total_episodes': collected_counts['total_episodes'],
        'recently_aired': recently_aired,
        'airing_soon': airing_soon,
        'upcoming_releases': upcoming_releases,
        'today': now.date(),
        'yesterday': (now - timedelta(days=1)).date(),
        'tomorrow': (now + timedelta(days=1)).date()
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
            episodeResults = web_scrape_tvshow(media_id, title, year, season)
            return jsonify(episodeResults)
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
    logs = get_recent_logs(500)  # Get the 500 most recent log entries
    return render_template('logs.html', logs=logs)

@app.route('/api/logs')
def api_logs():
    logs = get_recent_logs(500)
    return jsonify(logs)

def get_recent_logs(n):
    log_path = 'logs/debug.log'
    if not os.path.exists(log_path):
        return []
    with open(log_path, 'r') as f:
        logs = f.readlines()
    recent_logs = logs[-n:]  # Get the last n logs
    return [{'level': get_log_level(log), 'message': log.strip()} for log in recent_logs]

def get_log_level(log_entry):
    if ' - DEBUG - ' in log_entry:
        return 'debug'
    elif ' - INFO - ' in log_entry:
        return 'info'
    elif ' - WARNING - ' in log_entry:
        return 'warning'
    elif ' - ERROR - ' in log_entry:
        return 'error'
    elif ' - CRITICAL - ' in log_entry:
        return 'critical'
    else:
        return 'info'  # Default to info if level can't be determined

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

@app.route('/api/settings', methods=['POST'])
def update_settings():
    try:
        new_settings = request.json
        #logging.debug(f"Received new settings: {json.dumps(new_settings, indent=2)}")
        
        config = load_config()
        #logging.debug(f"Current config before update: {json.dumps(config, indent=2)}")
        
        # Update the config with new settings
        for section, settings in new_settings.items():
            if section not in config:
                config[section] = {}
            if section == 'Content Sources':
                # Update the Content Sources in the config dictionary
                config['Content Sources'] = settings
                #logging.debug(f"Updated Content Sources in config: {json.dumps(config['Content Sources'], indent=2)}")
            else:
                config[section].update(settings)
                #logging.debug(f"Updated {section} in config: {json.dumps(config[section], indent=2)}")
        
        #logging.debug(f"Config just before saving: {json.dumps(config, indent=2)}")
        
        # Save the updated config
        save_config(config)
        
        # Verify the saved config
        saved_config = load_config()
        #logging.debug(f"Saved config after update: {json.dumps(saved_config, indent=2)}")
        
        if saved_config != config:
            logging.warning("Saved config does not match updated config")
            # Add detailed comparison
            for section in config:
                if section not in saved_config:
                    logging.warning(f"Section {section} is missing in saved config")
                elif config[section] != saved_config[section]:
                    logging.warning(f"Section {section} differs in saved config")
                    logging.warning(f"Expected: {json.dumps(config[section], indent=2)}")
                    logging.warning(f"Actual: {json.dumps(saved_config[section], indent=2)}")
        
        return jsonify({"status": "success", "message": "Settings updated successfully"})
    except Exception as e:
        logging.error(f"Error updating settings: {str(e)}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500

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
    #logging.info(f"Rendering queues page. UpgradingQueue size: {len(upgrading_queue)}")
    return render_template('queues.html', queue_contents=queue_contents, upgrading_queue=upgrading_queue)

@app.route('/api/queue_contents')
def api_queue_contents():
    contents = queue_manager.get_queue_contents()
    # Ensure wake counts are included for Sleeping queue items
    if 'Sleeping' in contents:
        for item in contents['Sleeping']:
            item['wake_count'] = queue_manager.get_wake_count(item['id'])
    #logging.info(f"Queue contents: {contents}")  # Add this line
    return jsonify(contents)

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
        current_app.config['PROGRAM_RUNNING'] = True
        return jsonify({"status": "success", "message": "Program started"})
    else:
        return jsonify({"status": "error", "message": "Program is already running"})

@app.route('/api/reset_program', methods=['POST'])
def reset_program():
    global program_runner
    if program_runner is not None:
        program_runner.stop()
    program_runner = None
    current_app.config['PROGRAM_RUNNING'] = False
    return jsonify({"status": "success", "message": "Program reset"})

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
    data = request.json
    version_name = data.get('name')
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
    return jsonify({'success': True, 'version_id': version_name})

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

@app.route('/versions/duplicate', methods=['POST'])
def duplicate_version():
    data = request.json
    version_id = data.get('version_id')
    
    if not version_id:
        return jsonify({'success': False, 'error': 'No version ID provided'}), 400

    config = load_config()
    if 'Scraping' not in config or 'versions' not in config['Scraping'] or version_id not in config['Scraping']['versions']:
        return jsonify({'success': False, 'error': 'Version not found'}), 404

    new_version_id = f"{version_id} Copy"
    counter = 1
    while new_version_id in config['Scraping']['versions']:
        new_version_id = f"{version_id} Copy {counter}"
        counter += 1

    config['Scraping']['versions'][new_version_id] = config['Scraping']['versions'][version_id].copy()
    config['Scraping']['versions'][new_version_id]['display_name'] = new_version_id

    save_config(config)
    return jsonify({'success': True, 'new_version_id': new_version_id})

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

@app.route('/scraper_tester', methods=['GET', 'POST'])
def scraper_tester():
    if request.method == 'POST':
        if request.is_json:
            data = request.json
            search_term = data.get('search_term')
        else:
            search_term = request.form.get('search_term')
        
        if search_term:
            search_results = search_overseerr(search_term)
            app.logger.debug(f"Search results: {search_results}")
            
            # Fetch IMDB IDs for each result
            for result in search_results:
                app.logger.debug(f"Processing result: {result}")
                details = get_details(result)
                app.logger.debug(f"Details for result: {details}")
                
                if details:
                    imdb_id = details.get('externalIds', {}).get('imdbId', 'N/A')
                    result['imdbId'] = imdb_id
                    app.logger.debug(f"IMDB ID found: {imdb_id}")
                else:
                    result['imdbId'] = 'N/A'
                    app.logger.debug("No details found for this result")
            
            app.logger.debug(f"Final search results with IMDB IDs: {search_results}")
            return jsonify(search_results)
        else:
            return jsonify({'error': 'No search term provided'}), 400
    
    # GET request handling
    all_settings = get_all_settings()
    versions = all_settings.get('Scraping', {}).get('versions', {}).keys()
    
    # Log the versions for debugging
    app.logger.debug(f"Available versions: {list(versions)}")
    
    return render_template('scraper_tester.html', versions=versions)

@app.route('/get_scraping_versions', methods=['GET'])
def get_scraping_versions():
    try:
        config = load_config()
        versions = config.get('Scraping', {}).get('versions', {}).keys()
        return jsonify({'versions': list(versions)})
    except Exception as e:
        app.logger.error(f"Error getting scraping versions: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500

@app.route('/get_version_settings')
def get_version_settings_route():
    try:
        version = request.args.get('version')
        if not version:
            return jsonify({'error': 'No version provided'}), 400
        
        version_settings = get_version_settings(version)
        if not version_settings:
            return jsonify({'error': f'No settings found for version: {version}'}), 404
        
        # Ensure max_resolution is included in the settings
        if 'max_resolution' not in version_settings:
            version_settings['max_resolution'] = '1080p'  # or whatever the default should be
        
        return jsonify({version: version_settings})
    except Exception as e:
        app.logger.error(f"Error in get_version_settings: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500

@app.route('/get_item_details', methods=['POST'])
def get_item_details():
    item = request.json
    details = get_details(item)
    
    if details:
        # Ensure IMDB ID is included
        imdb_id = details.get('externalIds', {}).get('imdbId', '')
        
        response_data = {
            'imdb_id': imdb_id,
            'tmdb_id': str(details.get('id', '')),
            'title': details.get('title') if item['mediaType'] == 'movie' else details.get('name', ''),
            'year': details.get('releaseDate', '')[:4] if item['mediaType'] == 'movie' else details.get('firstAirDate', '')[:4],
            'mediaType': item['mediaType']
        }
        return jsonify(response_data)
    else:
        return jsonify({'error': 'Could not fetch details'}), 400

@app.route('/save_version_settings', methods=['POST'])
def save_version_settings():
    data = request.json
    version = data.get('version')
    settings = data.get('settings')

    if not version or not settings:
        return jsonify({'success': False, 'error': 'Invalid data provided'}), 400

    try:
        config = load_config()
        if 'Scraping' not in config:
            config['Scraping'] = {}
        if 'versions' not in config['Scraping']:
            config['Scraping']['versions'] = {}
        
        config['Scraping']['versions'][version] = settings
        save_config(config)
        
        return jsonify({'success': True})
    except Exception as e:
        app.logger.error(f"Error saving version settings: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/run_scrape', methods=['POST'])
def run_scrape():
    data = request.json
    logging.debug(f"Received scrape data: {data}")
    try:
        imdb_id = data.get('imdb_id', '')
        tmdb_id = data.get('tmdb_id', '')
        title = data['title']
        year = data.get('year')
        media_type = data['movie_or_episode']
        version = data['version']
        modified_settings = data.get('modifiedSettings', {})
        
        if media_type == 'episode':
            season = data.get('season')
            episode = data.get('episode')
            multi = data.get('multi', False)
        else:
            season = None
            episode = None
            multi = False

        year = int(year) if year else None

        logging.debug(f"Scraping with parameters: imdb_id={imdb_id}, tmdb_id={tmdb_id}, title={title}, year={year}, media_type={media_type}, version={version}, season={season}, episode={episode}, multi={multi}")

        # Load current config and get original version settings
        config = load_config()
        original_version_settings = config['Scraping']['versions'].get(version, {}).copy()
        
        # Run first scrape with current settings
        original_results, _ = scrape(
            imdb_id, tmdb_id, title, year, media_type, version, season, episode, multi
        )

        # Update version settings with modified settings
        updated_version_settings = original_version_settings.copy()
        updated_version_settings.update(modified_settings)

        # Save modified settings temporarily
        config['Scraping']['versions'][version] = updated_version_settings
        save_config(config)

        logging.debug(f"Original version settings: {original_version_settings}")
        logging.debug(f"Modified version settings: {updated_version_settings}")

        # Run second scrape with modified settings
        try:
            adjusted_results, _ = scrape(
                imdb_id, tmdb_id, title, year, media_type, version, season, episode, multi
            )
        finally:
            # Revert settings back to original
            config = load_config()
            config['Scraping']['versions'][version] = original_version_settings
            save_config(config)

        # Ensure score_breakdown is included in the results
        for result in original_results + adjusted_results:
            if 'score_breakdown' not in result:
                result['score_breakdown'] = {'total_score': result.get('score', 0)}

        return jsonify({
            'originalResults': original_results,
            'adjustedResults': adjusted_results
        })
    except Exception as e:
        logging.error(f"Error in run_scrape: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500

@app.route('/api/update_program_state', methods=['POST'])
def update_program_state():
    state = request.json.get('state')
    if state in ['Running', 'Initialized']:
        current_app.config['PROGRAM_RUNNING'] = (state == 'Running')
        return jsonify({"status": "success", "message": f"Program state updated to {state}"})
    else:
        return jsonify({"status": "error", "message": "Invalid state"}), 400

@app.route('/api/program_status', methods=['GET'])
def program_status():
    status = "Running" if current_app.config.get('PROGRAM_RUNNING', False) else "Initialized"
    return jsonify({"status": status})

if __name__ == '__main__':
    start_server()
