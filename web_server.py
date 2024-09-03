from flask import Flask, render_template, jsonify, redirect, url_for, request, session, send_from_directory, flash, Response, make_response, current_app
import traceback
from flask_session import Session
import threading
import time
from queue_manager import QueueManager
import logging
import os
from settings import get_all_settings, set_setting, get_setting, load_config, save_config, to_bool, ensure_trakt_auth
from collections import OrderedDict, defaultdict
from web_scraper import web_scrape, web_scrape_tvshow, process_media_selection, process_torrent_selection, get_available_versions, trending_movies, trending_shows
from debrid.real_debrid import add_to_real_debrid
import re
from datetime import datetime, timedelta
import sqlite3
from database import get_db_connection, get_collected_counts, remove_from_media_items, bulk_delete_by_imdb_id, get_recently_added_items, get_recently_added_items, create_tables, verify_database
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
from config_manager import add_content_source, delete_content_source, update_content_source, add_scraper, load_config, save_config, get_version_settings, update_all_content_sources, clean_notifications, get_content_source_settings
from settings_schema import SETTINGS_SCHEMA
from trakt.core import get_device_code, get_device_token
from scraper.scraper import scrape
from utilities.manual_scrape import search_overseerr, get_details
from settings import get_all_settings
import string
from itertools import groupby
from operator import itemgetter
from flask import current_app
from api_tracker import api, api_logger
from urllib.parse import urlparse
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from sqlalchemy import inspect, text
from pathlib import Path
from utilities.plex_functions import sync_run_get_recent_from_plex
import aiohttp
import asyncio
from content_checkers.overseerr import get_overseerr_details, get_overseerr_headers
import shutil
from utilities.debug_commands import get_and_add_all_collected_from_plex, get_and_add_recent_collected_from_plex, get_and_add_wanted_content, get_all_wanted_from_enabled_sources
from web_scraper import get_media_details, process_media_selection
import pickle
from flask.json import jsonify
from babelfish import Language
from metadata.metadata import get_overseerr_show_details, get_all_season_episode_counts, get_overseerr_cookies

CACHE_FILE = 'db_content/api_summary_cache.pkl'
# Add this at the global scope, outside of any function
app_start_time = time.time()

app = Flask(__name__)

app.config['SESSION_TYPE'] = 'filesystem'
Session(app)
app.secret_key = '9683650475'
queue_manager = QueueManager()
scraper_manager = ScraperManager()

# Ensure the directory for the database exists
db_directory = os.path.join(app.root_path, 'db_content')
os.makedirs(db_directory, exist_ok=True)

# Set the database URI
db_path = os.path.join(db_directory, 'users.db')
app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{db_path}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Ensure the directory is writable
if not os.access(db_directory, os.W_OK):
    raise PermissionError(f"The directory {db_directory} is not writable. Please check permissions.")

db = SQLAlchemy(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

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

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'rb') as f:
                loaded_cache = pickle.load(f)
                # Ensure the cache has the correct structure
                if not isinstance(loaded_cache, dict):
                    raise ValueError("Loaded cache is not a dictionary")
                for time_frame in ['hour', 'day', 'month']:
                    if time_frame not in loaded_cache or not isinstance(loaded_cache[time_frame], dict):
                        loaded_cache[time_frame] = {}
                if 'last_processed_line' not in loaded_cache:
                    loaded_cache['last_processed_line'] = 0
                return loaded_cache
        except (EOFError, ValueError, pickle.UnpicklingError) as e:
            logging.warning(f"Error loading cache file: {str(e)}. Creating a new cache.")
    return {'hour': {}, 'day': {}, 'month': {}, 'last_processed_line': 0}

def save_cache(cache_data):
    with open(CACHE_FILE, 'wb') as f:
        pickle.dump(cache_data, f)
# Initialize the cache when the server starts
try:
    cache = load_cache()
except Exception as e:
    logging.error(f"Failed to load cache: {str(e)}. Starting with an empty cache.")
    cache = {'hour': {}, 'day': {}, 'month': {}, 'last_processed_line': 0}

def program_is_running():
    global program_runner
    return program_runner.is_running() if program_runner else False

def onboarding_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_user_system_enabled():
            return f(*args, **kwargs)
        if current_user.is_authenticated and not current_user.onboarding_complete:
            next_step = get_next_onboarding_step()
            if next_step <= 5:  # Assuming 5 is the last step
                return redirect(url_for('onboarding_step', step=next_step))
        return f(*args, **kwargs)
    return decorated_function

def get_next_onboarding_step():
    # Load the current configuration
    config = load_config()
    
    # Step 1: Check if the admin user is set up
    if current_user.is_default:
        return 1
    
    # Step 2: Check if required settings are configured
    required_settings = [
        ('Plex', 'url'),
        ('Plex', 'token'),
        ('Overseerr', 'url'),
        ('Overseerr', 'api_key'),
        ('RealDebrid', 'api_key')
    ]
    
    for category, key in required_settings:
        if not get_setting(category, key):
            return 2
    
    # Step 3: Check if at least one scraper is configured
    if 'Scrapers' not in config or not config['Scrapers']:
        return 3
    
    # Step 4: Check if at least one content source is configured
    if 'Content Sources' not in config or not config['Content Sources']:
        return 4
    
    # If all steps are completed, return the final step (5)
    return 5

@app.context_processor
def inject_program_status():
    return dict(program_is_running=program_is_running)

# Add this after app initialization
@app.context_processor
def inject_version():
    try:
        with open('version.txt', 'r') as f:
            version = f.read().strip()
    except FileNotFoundError:
        version = "Unknown"
    return dict(version=version)
    
def is_user_system_enabled():
    config = load_config()
    return config.get('UI Settings', {}).get('enable_user_system', True)

def create_default_admin():
    # Check if there are any existing users
    if User.query.count() == 0:
        default_admin = User.query.filter_by(username='admin').first()
        if not default_admin:
            hashed_password = generate_password_hash('admin')
            default_admin = User(
                username='admin', 
                password=hashed_password, 
                role='admin', 
                is_default=True,
                onboarding_complete=False  # Set onboarding_complete to False
            )
            db.session.add(default_admin)
            db.session.commit()
            logging.info("Default admin account created with onboarding incomplete.")
        else:
            logging.info("Default admin already exists.")
    else:
        logging.info("Users already exist. Skipping default admin creation.")

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    is_default = db.Column(db.Boolean, default=False)
    onboarding_complete = db.Column(db.Boolean, default=False)

def perform_database_migration():

    logging.info("Performing database migration...")
    with app.app_context():
        inspector = inspect(db.engine)
        if not inspector.has_table("user"):
            # If the user table doesn't exist, create all tables
            db.create_all()
        else:
            # Check if onboarding_complete column exists
            columns = [c['name'] for c in inspector.get_columns('user')]
            if 'onboarding_complete' not in columns:
                # Add onboarding_complete column
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE user ADD COLUMN onboarding_complete BOOLEAN DEFAULT FALSE"))
                    conn.commit()
        
        # Commit the changes
        db.session.commit()

async def get_recent_from_plex(movie_limit=5, show_limit=5):
    plex_url = get_setting('Plex', 'url', '').rstrip('/')
    plex_token = get_setting('Plex', 'token', '')
    
    if not plex_url or not plex_token:
        return {'movies': [], 'shows': []}
    
    headers = {
        'X-Plex-Token': plex_token,
        'Accept': 'application/json'
    }

    async def fetch_metadata(session, item_key):
        metadata_url = f"{plex_url}{item_key}?includeGuids=1"
        async with session.get(metadata_url, headers=headers) as response:
            return await response.json()

    async with aiohttp.ClientSession() as session:
        # Get library sections
        async with session.get(f"{plex_url}/library/sections", headers=headers) as response:
            sections = await response.json()

        recent_movies = []
        recent_shows = {}

        for section in sections['MediaContainer']['Directory']:
            if section['type'] == 'movie':
                async with session.get(f"{plex_url}/library/sections/{section['key']}/recentlyAdded?X-Plex-Container-Start=0&X-Plex-Container-Size={movie_limit}", headers=headers) as response:
                    data = await response.json()
                    for item in data['MediaContainer'].get('Metadata', []):
                        metadata = await fetch_metadata(session, item['key'])
                        if 'MediaContainer' in metadata and 'Metadata' in metadata['MediaContainer']:
                            full_metadata = metadata['MediaContainer']['Metadata'][0]
                            tmdb_id = next((guid['id'] for guid in full_metadata.get('Guid', []) if guid['id'].startswith('tmdb://')), None)
                            if tmdb_id:
                                tmdb_id = tmdb_id.split('://')[1]
                                poster_url = await get_poster_url(session, tmdb_id, 'movie')
                                recent_movies.append({
                                    'title': item['title'],
                                    'year': item.get('year'),
                                    'added_at': datetime.fromtimestamp(int(item['addedAt'])).strftime('%Y-%m-%d %H:%M:%S'),
                                    'poster_url': poster_url
                                })
            elif section['type'] == 'show':
                async with session.get(f"{plex_url}/library/sections/{section['key']}/recentlyAdded?X-Plex-Container-Start=0&X-Plex-Container-Size=100", headers=headers) as response:
                    data = await response.json()
                    for item in data['MediaContainer'].get('Metadata', []):
                        if item['type'] == 'episode' and len(recent_shows) < show_limit:
                            show_title = item['grandparentTitle']
                            if show_title not in recent_shows:
                                show_metadata = await fetch_metadata(session, item['grandparentKey'])
                                if 'MediaContainer' in show_metadata and 'Metadata' in show_metadata['MediaContainer']:
                                    full_show_metadata = show_metadata['MediaContainer']['Metadata'][0]
                                    tmdb_id = next((guid['id'] for guid in full_show_metadata.get('Guid', []) if guid['id'].startswith('tmdb://')), None)
                                    if tmdb_id:
                                        tmdb_id = tmdb_id.split('://')[1]
                                        poster_url = await get_poster_url(session, tmdb_id, 'tv')
                                        recent_shows[show_title] = {
                                            'title': show_title,
                                            'added_at': datetime.fromtimestamp(int(item['addedAt'])).strftime('%Y-%m-%d %H:%M:%S'),
                                            'poster_url': poster_url,
                                            'seasons': set()
                                        }
                            if show_title in recent_shows:
                                recent_shows[show_title]['seasons'].add(item['parentIndex'])
                                recent_shows[show_title]['added_at'] = max(
                                    recent_shows[show_title]['added_at'],
                                    datetime.fromtimestamp(int(item['addedAt'])).strftime('%Y-%m-%d %H:%M:%S')
                                )
                            if len(recent_shows) == show_limit:
                                break

        recent_shows = list(recent_shows.values())
        for show in recent_shows:
            show['seasons'] = sorted(show['seasons'])
        recent_shows.sort(key=lambda x: x['added_at'], reverse=True)

    return {
        'movies': recent_movies[:movie_limit],
        'shows': recent_shows[:show_limit]
    }

def sync_run_get_recent_from_plex():
    return asyncio.run(get_recent_from_plex())

@app.template_filter('isinstance')
def isinstance_filter(value, class_name):
    return isinstance(value, getattr(datetime, class_name, type(None)))

@app.template_filter('is_infinite')
def is_infinite(value):
    return value == float('inf')

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_user_system_enabled():
            return f(*args, **kwargs)
        if not current_user.is_authenticated or current_user.role != 'admin':
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def user_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_user_system_enabled():
            return f(*args, **kwargs)
        if not current_user.is_authenticated:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@login_manager.user_loader
def load_user(user_id):
    if is_user_system_enabled():
        return User.query.get(int(user_id))
    return None

def initialize_app():
    with app.app_context():
        inspector = inspect(db.engine)
        if not inspector.has_table("user"):
            db.create_all()
        else:
            columns = [c['name'] for c in inspector.get_columns('user')]
            if 'is_default' not in columns:
                with db.engine.connect() as conn:
                    conn.execute(db.text('ALTER TABLE user ADD COLUMN is_default BOOLEAN'))
                    conn.commit()
        create_default_admin()

@app.route('/api/check_program_conditions')
@login_required
@admin_required
def check_program_conditions():
    config = load_config()
    scrapers_enabled = any(scraper.get('enabled', False) for scraper in config.get('Scrapers', {}).values())
    content_sources_enabled = any(source.get('enabled', False) for source in config.get('Content Sources', {}).values())
    
    required_settings = [
        ('Plex', 'url'),
        ('Plex', 'token'),
        ('Overseerr', 'url'),
        ('Overseerr', 'api_key'),
        ('RealDebrid', 'api_key')
    ]
    
    missing_fields = []
    for category, key in required_settings:
        value = get_setting(category, key)
        if not value:
            missing_fields.append(f"{category}.{key}")
    
    required_settings_complete = len(missing_fields) == 0

    return jsonify({
        'canRun': scrapers_enabled and content_sources_enabled and required_settings_complete,
        'scrapersEnabled': scrapers_enabled,
        'contentSourcesEnabled': content_sources_enabled,
        'requiredSettingsComplete': required_settings_complete,
        'missingFields': missing_fields
    })

@app.route('/onboarding')
@login_required
def onboarding():
    return render_template('onboarding.html', is_onboarding=True)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if not is_user_system_enabled():
        return redirect(url_for('statistics'))
    
    if current_user.is_authenticated:
        if not current_user.onboarding_complete:
            next_step = get_next_onboarding_step()
            if next_step <= 5:  # Assuming 5 is the last step
                return redirect(url_for('onboarding_step', step=next_step))
        return redirect(url_for('statistics'))

    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            login_user(user)
            logging.info(f"User {user.username} logged in. Onboarding complete: {user.onboarding_complete}")
            if user.is_default:
                return redirect(url_for('onboarding_step', step=1))
            if not user.onboarding_complete:
                logging.info(f"Redirecting user {user.username} to onboarding")
                return redirect(url_for('onboarding_step', step=1))
            return redirect(url_for('statistics'))
        else:
            flash('Invalid username or password.', 'error')
    
    return render_template('login.html')

@app.route('/onboarding/step/<int:step>', methods=['GET', 'POST'])
@login_required
def onboarding_step(step):
    if step < 1 or step > 5:
        abort(404)
    
    config = load_config()
    can_proceed = False

    if step == 1:
        admin_created = not current_user.is_default
        can_proceed = admin_created

        if request.method == 'POST':
            new_username = request.form['new_username']
            new_password = request.form['new_password']
            confirm_password = request.form['confirm_password']
            if new_password == confirm_password:
                try:
                    current_user.username = new_username
                    current_user.password = generate_password_hash(new_password)
                    current_user.is_default = False
                    db.session.commit()
                    return jsonify({'success': True})
                except Exception as e:
                    return jsonify({'success': False, 'error': str(e)})
            else:
                return jsonify({'success': False, 'error': 'Passwords do not match'})

        return render_template('onboarding_step_1.html', current_step=step, can_proceed=can_proceed, admin_created=admin_created, is_onboarding=True)
       
    if step == 2:
        required_settings = [
            ('Plex', 'url'),
            ('Plex', 'token'),
            ('Plex', 'shows_libraries'),
            ('Plex', 'movie_libraries'),
            ('Overseerr', 'url'),
            ('Overseerr', 'api_key'),
            ('RealDebrid', 'api_key')
        ]

        if request.method == 'POST':
            try:
                config = load_config()
                config['Plex'] = {
                    'url': request.form['plex_url'],
                    'token': request.form['plex_token'],
                    'shows_libraries': request.form['shows_libraries'],
                    'movie_libraries': request.form['movie_libraries']
                }
                config['Overseerr'] = {
                    'url': request.form['overseerr_url'],
                    'api_key': request.form['overseerr_api_key']
                }
                config['RealDebrid'] = {
                    'api_key': request.form['realdebrid_api_key']
                }
                save_config(config)
                
                # Check if all required settings are now present
                can_proceed = all(get_setting(category, key) for category, key in required_settings)
                
                return jsonify({'success': True, 'can_proceed': can_proceed})
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)})
        
        # For GET requests, load existing settings if any
        config = load_config()
        can_proceed = all(get_setting(category, key) for category, key in required_settings)
        
        return render_template('onboarding_step_2.html', 
                               current_step=step, 
                               can_proceed=can_proceed,
                               settings=config, is_onboarding=True)
    if step == 3:
        config = load_config()
        can_proceed = 'Scrapers' in config and bool(config['Scrapers'])
        return render_template('onboarding_step_3.html', 
                               current_step=step, 
                               can_proceed=can_proceed, 
                               settings=config, 
                               SETTINGS_SCHEMA=SETTINGS_SCHEMA, is_onboarding=True)

    if step == 4:
        config = load_config()
        can_proceed = 'Content Sources' in config and bool(config['Content Sources'])
        return render_template('onboarding_step_4.html', 
                               current_step=step, 
                               can_proceed=can_proceed, 
                               settings=config, 
                               SETTINGS_SCHEMA=SETTINGS_SCHEMA, is_onboarding=True)

    elif step == 5:
        can_proceed = True  # Always allow finishing the onboarding process
        return render_template('onboarding_step_5.html', current_step=step, can_proceed=can_proceed, is_onboarding=True)


@app.route('/onboarding/complete', methods=['POST'])
@login_required
def complete_onboarding():
    try:
        current_user.onboarding_complete = True
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        app.logger.error(f"Error completing onboarding: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
    
@app.route('/onboarding/update_can_proceed', methods=['POST'])
@login_required
def update_can_proceed():
    data = request.json
    step = data.get('step')
    can_proceed = data.get('can_proceed')
    
    if step in [1, 2, 3, 4]:
        session[f'onboarding_step_{step}_can_proceed'] = can_proceed
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'error': 'Invalid step'}), 400

def update_required_settings(form_data):
    config = load_config()
    config['Plex']['url'] = form_data.get('plex_url')
    config['Plex']['token'] = form_data.get('plex_token')
    config['Plex']['shows_libraries'] = form_data.get('shows_libraries')
    config['Plex']['movies_libraries'] = form_data.get('movies_libraries')
    config['Overseerr']['url'] = form_data.get('overseerr_url')
    config['Overseerr']['api_key'] = form_data.get('overseerr_api_key')
    config['RealDebrid']['api_key'] = form_data.get('realdebrid_api_key')
    save_config(config)

def add_scraper_onboarding(form_data):
    scraper_type = form_data.get('scraper_type')
    scraper_config = {
        'enabled': True,
    }
    add_scraper(scraper_type, scraper_config)

def add_content_source_onboarding(form_data):
    source_type = form_data.get('source_type')
    source_config = {
        'enabled': True,
        'display_name': form_data.get('source_display_name'),
        'versions': form_data.getlist('source_versions')
    }
    add_content_source(source_type, source_config)

@app.route('/setup_admin', methods=['GET', 'POST'])
@login_required
def setup_admin():
    if not current_user.is_default:
        return redirect(url_for('onboarding_step', step=1))
    if request.method == 'POST':
        new_username = request.form['new_username']
        new_password = request.form['new_password']
        confirm_password = request.form['confirm_password']
        if new_password != confirm_password:
            flash('Passwords do not match.', 'error')
        else:
            existing_user = User.query.filter_by(username=new_username).first()
            if existing_user and existing_user.id != current_user.id:
                flash('Username already exists.', 'error')
            else:
                try:
                    # Delete all default admin accounts
                    User.query.filter_by(is_default=True).delete()
                    
                    # Create the new admin account
                    new_admin = User(username=new_username, 
                                     password=generate_password_hash(new_password),
                                     role='admin',
                                     is_default=False,
                                     onboarding_complete=False)  # Set onboarding_complete to False
                    db.session.add(new_admin)
                    db.session.commit()
                    
                    # Log out the current user (original admin) and log in the new admin
                    logout_user()
                    login_user(new_admin)
                    
                    # Redirect to the first onboarding step
                    return redirect(url_for('onboarding_step', step=1))
                except Exception as e:
                    db.session.rollback()
                    flash(f'An error occurred: {str(e)}', 'error')
                    app.logger.error(f"Error in setup_admin: {str(e)}", exc_info=True)
    return render_template('setup_admin.html', is_onboarding=True)

@app.route('/change_password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        new_password = request.form['new_password']
        confirm_password = request.form['confirm_password']
        if new_password == confirm_password:
            current_user.password = generate_password_hash(new_password)
            current_user.is_default = False
            db.session.commit()
            flash('Password changed successfully.', 'success')
            return redirect(url_for('statistics'))
        else:
            flash('Passwords do not match.', 'error')
    return render_template('change_password.html')

# Modify the manage_users route
@app.route('/manage_users')
@admin_required
@onboarding_required
def manage_users():
    if not is_user_system_enabled():
        flash('User management is disabled.', 'error')
        return redirect(url_for('statistics'))
    users = User.query.all()
    return render_template('manage_users.html', users=users)

@app.route('/add_user', methods=['POST'])
@admin_required
def add_user():
    username = request.form['username']
    password = request.form['password']
    role = request.form['role']
    
    existing_user = User.query.filter_by(username=username).first()
    if existing_user:
        return jsonify({'success': False, 'error': 'Username already exists.'})
    else:
        hashed_password = generate_password_hash(password)
        new_user = User(username=username, password=hashed_password, role=role)
        db.session.add(new_user)
        db.session.commit()
        return jsonify({'success': True})

@app.route('/delete_user/<int:user_id>', methods=['POST'])
@admin_required
def delete_user(user_id):
    if current_user.role != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    user = User.query.get(user_id)
    if not user:
        return jsonify({'success': False, 'error': 'User not found'}), 404

    if user.username == 'admin':
        return jsonify({'success': False, 'error': 'Cannot delete admin user'}), 400

    try:
        db.session.delete(user)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error deleting user: {str(e)}")
        return jsonify({'success': False, 'error': 'Database error'}), 500

# Modify the register route
@app.route('/register', methods=['GET', 'POST'])
def register():
    if not is_user_system_enabled():
        return redirect(url_for('statistics'))
    
    if current_user.is_authenticated:
        return redirect(url_for('statistics'))
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        existing_user = User.query.filter_by(username=username).first()
        if existing_user:
            flash('Username already exists.', 'error')
            return redirect(url_for('register'))
        hashed_password = generate_password_hash(password)
        new_user = User(username=username, password=hashed_password)
        if User.query.count() == 0:
            new_user.role = 'admin'
        db.session.add(new_user)
        db.session.commit()
        login_user(new_user)
        flash('Registered successfully.', 'success')
        return redirect(url_for('statistics'))
    return render_template('register.html')

# Modify the logout route
@app.route('/logout')
@login_required
def logout():
    if not is_user_system_enabled():
        return redirect(url_for('statistics'))
    logout_user()
    return redirect(url_for('login'))

def summarize_api_calls(time_frame):
    log_path = 'logs/api_calls.log'
    summary = defaultdict(lambda: defaultdict(int))
    
    with open(log_path, 'r') as f:
        for line in f:
            match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - API Call: (\w+) (.*) - Domain: (.*)', line)
            if match:
                timestamp, method, url, domain = match.groups()
                dt = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S,%f')
                
                if time_frame == 'hour':
                    key = dt.strftime('%Y-%m-%d %H:00')
                elif time_frame == 'day':
                    key = dt.strftime('%Y-%m-%d')
                elif time_frame == 'month':
                    key = dt.strftime('%Y-%m')
                
                summary[key][domain] += 1
    
    return dict(summary)

def get_cached_summary(time_frame):
    current_time = datetime.now()
    if time_frame in cache:
        last_update, data = cache[time_frame]
        if current_time - last_update < timedelta(hours=1):
            return data
    
    data = summarize_api_calls(time_frame)
    cache[time_frame] = (current_time, data)
    save_cache(cache)
    return data

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
    return dict(render_settings=render_settings, render_content_sources=render_content_sources, is_user_system_enabled=is_user_system_enabled)

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

@app.route('/content-sources/content')
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

@app.route('/onboarding/content_sources/add', methods=['POST'])
def add_onboarding_content_source():
    data = request.json
    source_type = data.get('type')
    source_config = data.get('config')
    
    if not source_type or not source_config:
        return jsonify({'success': False, 'error': 'Invalid content source data'}), 400

    try:
        new_source_id = add_content_source(source_type, source_config)
        
        # Mark onboarding as complete
        current_user.onboarding_complete = True
        db.session.commit()

        # Log the addition of the new content source
        app.logger.info(f"Added new content source during onboarding: {new_source_id}")

        return jsonify({'success': True, 'source_id': new_source_id})
    except Exception as e:
        app.logger.error(f"Error adding content source during onboarding: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/onboarding/content_sources/get', methods=['GET'])
def get_onboarding_content_sources():
    config = load_config()
    content_source_types = list(SETTINGS_SCHEMA['Content Sources']['schema'].keys())
    content_sources = config.get('Content Sources', {})
    logging.debug(f"Retrieved content sources: {content_sources}")
    return jsonify({
        'content_sources': content_sources,
        'source_types': content_source_types,
        'settings': SETTINGS_SCHEMA['Content Sources']['schema']
    })

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

@app.route('/onboarding/scrapers/add', methods=['POST'])
def add_onboarding_scraper():
    data = request.json
    scraper_type = data.get('type')
    scraper_config = data.get('config')
    
    if not scraper_type or not scraper_config:
        return jsonify({'success': False, 'error': 'Invalid scraper data'}), 400

    config = load_config()
    scrapers = config.get('Scrapers', {})
    
    # Generate a unique ID for the new scraper
    scraper_id = f"{scraper_type}_{len([s for s in scrapers if s.startswith(scraper_type)]) + 1}"
    
    scrapers[scraper_id] = scraper_config
    config['Scrapers'] = scrapers
    save_config(config)

    # Log the addition of the new scraper
    app.logger.info(f"Added new scraper during onboarding: {scraper_id}")

    return jsonify({'success': True, 'scraper_id': scraper_id})

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

@app.route('/onboarding/scrapers/get', methods=['GET'])
def get_onboarding_scrapers():
    config = load_config()
    scraper_types = scraper_manager.get_scraper_types()
    return jsonify({
        'scrapers': config.get('Scrapers', {}),
        'scraper_types': scraper_types
    })

@app.route('/get_content_source_types', methods=['GET'])
def get_content_source_types():
    content_sources = SETTINGS_SCHEMA['Content Sources']['schema']
    return jsonify({
        'source_types': list(content_sources.keys()),
        'settings': content_sources
    })

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
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Get all column names
        cursor.execute("PRAGMA table_info(media_items)")
        all_columns = [column[1] for column in cursor.fetchall()]

        # Define the default columns
        default_columns = [
            'imdb_id', 'title', 'year', 'release_date', 'state', 'type',
            'season_number', 'episode_number', 'collected_at', 'version'
        ]

        # Get or set selected columns
        if request.method == 'POST':
            selected_columns = request.form.getlist('columns')
            session['selected_columns'] = selected_columns
        else:
            selected_columns = session.get('selected_columns')

        # If no columns are selected, use the default columns
        if not selected_columns:
            selected_columns = [col for col in default_columns if col in all_columns]
            if not selected_columns:
                selected_columns = ['id']  # Fallback to ID if none of the default columns exist

        # Ensure at least one column is selected
        if not selected_columns:
            selected_columns = ['id']

        # Get filter and sort parameters
        filter_column = request.args.get('filter_column', '')
        filter_value = request.args.get('filter_value', '')
        sort_column = request.args.get('sort_column', 'id')  # Default sort by id
        sort_order = request.args.get('sort_order', 'asc')
        content_type = request.args.get('content_type', 'movie')  # Default to 'movie'
        current_letter = request.args.get('letter', 'A')

        # Validate sort_column
        if sort_column not in all_columns:
            sort_column = 'id'  # Fallback to 'id' if invalid column is provided

        # Validate sort_order
        if sort_order.lower() not in ['asc', 'desc']:
            sort_order = 'asc'  # Fallback to 'asc' if invalid order is provided

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

        # Construct the ORDER BY clause safely
        order_clause = f"ORDER BY {sort_column} {sort_order}"

        # Ensure 'id' is always included in the query, even if not displayed
        query_columns = list(set(selected_columns + ['id']))
        
        # Construct the final query
        query = f"SELECT {', '.join(query_columns)} FROM media_items"
        if where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)
        query += f" {order_clause}"

        # Log the query and parameters for debugging
        logging.debug(f"Executing query: {query}")
        logging.debug(f"Query parameters: {params}")

        # Execute the query
        cursor.execute(query, params)
        items = cursor.fetchall()

        # Log the number of items fetched
        logging.debug(f"Fetched {len(items)} items from the database")

        conn.close()

        # Convert items to a list of dictionaries, always including 'id'
        items = [dict(zip(query_columns, item)) for item in items]


        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({
                'table': render_template('database_table.html', 
                                        items=items, 
                                        all_columns=all_columns,
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
        
    except sqlite3.Error as e:
        logging.error(f"SQLite error in database route: {str(e)}")
        items = []
        flash(f"Database error: {str(e)}", "error")
    except Exception as e:
        logging.error(f"Unexpected error in database route: {str(e)}")
        items = []
        flash("An unexpected error occurred. Please try again later.", "error")

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
            error_message = "No suitable video files found in the torrent."
            logging.error(f"Failed to add torrent to Real-Debrid: {error_message}")
            return jsonify({'error': error_message}), 500

    except Exception as e:
        error_message = str(e)
        logging.error(f"Error adding torrent to Real-Debrid: {error_message}")
        return jsonify({'error': f'An error occurred while adding to Real-Debrid: {error_message}'}), 500

def format_date(date_string):
    if not date_string:
        return ''
    try:
        date = datetime.fromisoformat(date_string)
        return date.strftime('%Y-%m-%d')
    except ValueError:
        return date_string

def format_time(date_input):
    if not date_input:
        return ''
    try:
        if isinstance(date_input, str):
            date = datetime.fromisoformat(date_input.rstrip('Z'))  # Remove 'Z' if present
        elif isinstance(date_input, datetime):
            date = date_input
        else:
            return ''
        return date.strftime('%H:%M:%S')
    except ValueError:
        return ''
    
def format_datetime_preference(date_input, use_24hour_format):
    if not date_input:
        return ''
    try:
        if isinstance(date_input, str):
            date = datetime.fromisoformat(date_input.rstrip('Z'))  # Remove 'Z' if present
        elif isinstance(date_input, datetime):
            date = date_input
        else:
            return str(date_input)
        
        now = datetime.now()
        today = now.date()
        yesterday = today - timedelta(days=1)
        tomorrow = today + timedelta(days=1)

        if date.date() == today:
            day_str = "Today"
        elif date.date() == yesterday:
            day_str = "Yesterday"
        elif date.date() == tomorrow:
            day_str = "Tomorrow"
        else:
            day_str = date.strftime("%a, %d %b %Y")

        time_format = "%H:%M" if use_24hour_format else "%I:%M %p"
        formatted_time = date.strftime(time_format)
        
        # Remove leading zero from hour in 12-hour format
        if not use_24hour_format:
            formatted_time = formatted_time.lstrip("0")
        
        return f"{day_str} {formatted_time}"
    except ValueError:
        return str(date_input)  # Return original string if parsing fails

@app.route('/statistics')
@user_required
@onboarding_required
def statistics():
    os.makedirs('db_content', exist_ok=True)

    start_time = time.time()

    uptime = int(time.time() - app_start_time)

    collected_counts = get_collected_counts()
    recently_aired, airing_soon = get_recently_aired_and_airing_soon()
    upcoming_releases = get_upcoming_releases()
    now = datetime.now()
    
    # Fetch recently added items from the database
    recently_added_start = time.time()
    recently_added = asyncio.run(get_recently_added_items(movie_limit=5, show_limit=5))
    recently_added_end = time.time()
    
    cookie_value = request.cookies.get('use24HourFormat')
    use_24hour_format = cookie_value == 'true' if cookie_value is not None else True
    
    # Format times for recently aired and airing soon
    for item in recently_aired + airing_soon:
        item['formatted_time'] = format_datetime_preference(item['air_datetime'], use_24hour_format)
    
    # Format times for upcoming releases (if they have time information)
    for item in upcoming_releases:
        item['formatted_time'] = format_datetime_preference(item['release_date'], use_24hour_format)

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
        'tomorrow': (now + timedelta(days=1)).date(),
        'recently_added_movies': recently_added['movies'],
        'recently_added_shows': recently_added['shows'],
        'use_24hour_format': use_24hour_format,
        'recently_aired': recently_aired,
        'airing_soon': airing_soon,
        'upcoming_releases': upcoming_releases,
        'timezone': time.tzname[0]
    }

    end_time = time.time()
    total_time = end_time - start_time

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify(stats)
    else:
        return render_template('statistics.html', stats=stats)
        
@app.route('/set_time_preference', methods=['POST'])
def set_time_preference():
    data = request.json
    use_24hour_format = data.get('use24HourFormat', True)
    
    # Format times with the new preference
    recently_aired, airing_soon = get_recently_aired_and_airing_soon()
    upcoming_releases = get_upcoming_releases()
    
    for item in recently_aired + airing_soon:
        item['formatted_time'] = format_datetime_preference(item['air_datetime'], use_24hour_format)
    
    for item in upcoming_releases:
        item['formatted_time'] = format_datetime_preference(item['release_date'], use_24hour_format)
    
    response = make_response(jsonify({
        'status': 'OK', 
        'use24HourFormat': use_24hour_format,
        'recently_aired': recently_aired,
        'airing_soon': airing_soon,
        'upcoming_releases': upcoming_releases
    }))
    response.set_cookie('use24HourFormat', 
                        str(use_24hour_format).lower(), 
                        max_age=31536000,  # 1 year
                        path='/',  # Ensure cookie is available for entire site
                        httponly=False)  # Allow JavaScript access
    return response

@app.route('/movies_trending', methods=['GET', 'POST'])
def movies_trending():
    versions = get_available_versions()
    if request.method == 'GET':
        trendingMovies = trending_movies()
        if trendingMovies:
            return jsonify(trendingMovies)
        else:
            return jsonify({'error': 'Error restrieving Trakt Trending Movies'})
    return render_template('scraper.html', versions=versions)

@app.route('/shows_trending', methods=['GET', 'POST'])
def shows_trending():
    versions = get_available_versions()
    if request.method == 'GET':
        trendingShows = trending_shows()
        if trendingShows:
            return jsonify(trendingShows)
        else:
            return jsonify({'error': 'Error restrieving Trakt Trending Shows'})
    return render_template('scraper.html', versions=versions)

@app.route('/scraper', methods=['GET', 'POST'])
@user_required
@onboarding_required
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

        # Fetch detailed information from Overseerr
        details = get_media_details(media_id, media_type)

        # Extract keywords and genres
        genres = details.get('keywords', [])

        logging.info(f"Retrieved genres: {genres}")

        logging.info(f"Selecting media: {media_id}, {title}, {year}, {media_type}, S{season or 'None'}E{episode or 'None'}, multi={multi}, version={version}, genres={genres}")

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

        torrent_results, cache_status = process_media_selection(media_id, title, year, media_type, season, episode, multi, version, genres)
        
        if not torrent_results:
            logging.warning("No torrent results found")
            return jsonify({'torrent_results': []})

        cached_results = []
        for result in torrent_results:
            result_hash = result.get('hash')
            if result_hash:
                is_cached = cache_status.get(result_hash, False)
                result['cached'] = 'RD' if is_cached else ''
            else:
                result['cached'] = ''
            cached_results.append(result)

        logging.info(f"Processed {len(cached_results)} results")
        return jsonify({'torrent_results': cached_results})
    except Exception as e:
        logging.error(f"Error in select_media: {str(e)}", exc_info=True)
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
@admin_required
@onboarding_required
def logs():
    logs = get_recent_logs(500)  # Get the 500 most recent log entries
    return render_template('logs.html', logs=logs)

@app.route('/api/logs')
@admin_required
def api_logs():
    lines = request.args.get('lines', default=250, type=int)  # Default to 250 logs
    download = request.args.get('download', default='false').lower() == 'true'
    since = request.args.get('since')
      
    try:
        logs = get_recent_logs(lines, since)
        
        if download:
            log_content = ''
            for log in logs:
                log_content += f"{log['timestamp']} - {log['level']} - {log['message']}\n"
            
            return Response(
                log_content,
                mimetype="text/plain",
                headers={"Content-disposition": "attachment; filename=debug.log"}
            )
        else:
            return jsonify(logs)
    except Exception as e:
        app.logger.error(f"Error in api_logs: {str(e)}", exc_info=True)
        return jsonify({'error': 'An error occurred while fetching logs'}), 500

def get_recent_logs(n, since=None):
    log_path = 'logs/debug.log'
    if not os.path.exists(log_path):
        return []
    with open(log_path, 'r') as f:
        logs = f.readlines()
    
    parsed_logs = [parse_log_line(log.strip()) for log in logs]
    
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
            parsed_logs = [
                log for log in parsed_logs 
                if log['timestamp'] and datetime.fromisoformat(log['timestamp']) > since_dt
            ]
        except ValueError as e:
            app.logger.error(f"Error parsing timestamp: {e}")
            # If there's an error parsing the timestamp, return the last n logs
            return parsed_logs[-n:]
    
    return parsed_logs[-n:]  # Return at most n logs

def parse_log_line(line):
    parts = line.split(' - ', 3)
    if len(parts) == 4:
        timestamp, module, level, message = parts
        try:
            # Validate the timestamp
            datetime.fromisoformat(timestamp)
        except ValueError:
            timestamp = ''  # Set to empty string if invalid
        
        level = level.strip().lower()  # Normalize the level
        return {'timestamp': timestamp, 'level': level, 'message': f"{module} - {message}"}
    else:
        return {'timestamp': '', 'level': 'info', 'message': line}
    
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

@app.route('/notifications/delete', methods=['POST'])
def delete_notification():
    try:
        notification_id = request.json.get('notification_id')
        if not notification_id:
            return jsonify({'success': False, 'error': 'No notification ID provided'}), 400

        config = load_config()
        if 'Notifications' in config and notification_id in config['Notifications']:
            del config['Notifications'][notification_id]
            save_config(config)
            logging.info(f"Notification {notification_id} deleted successfully")
            return jsonify({'success': True})
        else:
            logging.warning(f"Failed to delete notification: {notification_id}")
            return jsonify({'success': False, 'error': 'Notification not found'}), 404
    except Exception as e:
        logging.error(f"Error deleting notification: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/notifications/add', methods=['POST'])
def add_notification():
    try:
        notification_data = request.json
        if not notification_data or 'type' not in notification_data:
            return jsonify({'success': False, 'error': 'Invalid notification data'}), 400

        config = load_config()
        if 'Notifications' not in config:
            config['Notifications'] = {}

        notification_type = notification_data['type']
        existing_count = sum(1 for key in config['Notifications'] if key.startswith(f"{notification_type}_"))
        notification_id = f"{notification_type}_{existing_count + 1}"

        notification_title = notification_type.replace('_', ' ').title()

        config['Notifications'][notification_id] = {
            'type': notification_type,
            'enabled': True,
            'title': notification_title
        }

        # Add default values based on the notification type
        if notification_type == 'Telegram':
            config['Notifications'][notification_id].update({
                'bot_token': '',
                'chat_id': ''
            })
        elif notification_type == 'Discord':
            config['Notifications'][notification_id].update({
                'webhook_url': ''
            })
        elif notification_type == 'Email':
            config['Notifications'][notification_id].update({
                'smtp_server': '',
                'smtp_port': 587,
                'smtp_username': '',
                'smtp_password': '',
                'from_address': '',
                'to_address': ''
            })

        save_config(config)

        logging.info(f"Notification {notification_id} added successfully")
        return jsonify({'success': True, 'notification_id': notification_id})
    except Exception as e:
        logging.error(f"Error adding notification: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/notifications/content', methods=['GET'])
def notifications_content():
    try:
        config = load_config()
        notification_settings = config.get('Notifications', {})
        
        # Sort notifications by type and then by number
        sorted_notifications = sorted(
            notification_settings.items(),
            key=lambda x: (x[1]['type'], int(x[0].split('_')[-1]))
        )
        
        html_content = render_template(
            'settings_tabs/notifications_content.html',
            notification_settings=dict(sorted_notifications),
            settings_schema=SETTINGS_SCHEMA
        )
        
        return jsonify({
            'status': 'success',
            'html': html_content
        })
    except Exception as e:
        app.logger.error(f"Error generating notifications content: {str(e)}", exc_info=True)
        return jsonify({
            'status': 'error',
            'message': f'An error occurred while generating notifications content: {str(e)}',
            'traceback': traceback.format_exc()
        }), 500

@app.route('/settings', methods=['GET'])
@admin_required
@onboarding_required
def settings():
    try:
        config = load_config()
        config = clean_notifications(config)  # Clean notifications before rendering
        scraper_types = list(scraper_manager.scraper_settings.keys())
        source_types = list(SETTINGS_SCHEMA['Content Sources']['schema'].keys())

        # Fetch content source settings
        content_source_settings_response = get_content_source_settings_route()
        if isinstance(content_source_settings_response, Response):
            content_source_settings = content_source_settings_response.get_json()
        else:
            content_source_settings = content_source_settings_response        
            
        # Fetch scraping versions
        scraping_versions_response = get_scraping_versions()
        if isinstance(scraping_versions_response, Response):
            scraping_versions = scraping_versions_response.get_json()['versions']
        else:
            scraping_versions = scraping_versions_response['versions']

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

        # Ensure 'UI Settings' exists in the config
        if 'UI Settings' not in config:
            config['UI Settings'] = {}
        
        # Ensure 'enable_user_system' exists in 'UI Settings'
        if 'enable_user_system' not in config['UI Settings']:
            config['UI Settings']['enable_user_system'] = True  # Default to True
        
        
        # Ensure 'Content Sources' exists in the config
        if 'Content Sources' not in config:
            config['Content Sources'] = {}
        
        # Ensure each content source is a dictionary
        for source, source_config in config['Content Sources'].items():
            if not isinstance(source_config, dict):
                config['Content Sources'][source] = {}

        # Initialize notification_settings
        if 'Notifications' not in config:
            config['Notifications'] = {
                'Telegram': {'enabled': False, 'bot_token': '', 'chat_id': ''},
                'Discord': {'enabled': False, 'webhook_url': ''},
                'Email': {
                    'enabled': False,
                    'smtp_server': '',
                    'smtp_port': 587,
                    'smtp_username': '',
                    'smtp_password': '',
                    'from_address': '',
                    'to_address': ''
                }
            }

        return render_template('settings_base.html', 
                               settings=config, 
                               notification_settings=config['Notifications'],
                               scraper_types=scraper_types, 
                               scraper_settings=scraper_manager.scraper_settings,
                               source_types=source_types,
                               content_source_settings=content_source_settings,
                               scraping_versions=scraping_versions,
                               settings_schema=SETTINGS_SCHEMA)
    except Exception as e:
        app.logger.error(f"Error in settings route: {str(e)}", exc_info=True)
        return render_template('error.html', error_message="An error occurred while loading settings."), 500

@app.route('/api/program_settings', methods=['GET'])
@admin_required
def api_program_settings():
    try:
        config = load_config()
        program_settings = {
            'Scrapers': config.get('Scrapers', {}),
            'Content Sources': config.get('Content Sources', {}),
            'Plex': {
                'url': config.get('Plex', {}).get('url', ''),
                'token': config.get('Plex', {}).get('token', '')
            },
            'Overseerr': {
                'url': config.get('Overseerr', {}).get('url', ''),
                'api_key': config.get('Overseerr', {}).get('api_key', '')
            },
            'RealDebrid': {
                'api_key': config.get('RealDebrid', {}).get('api_key', '')
            }
        }
        return jsonify(program_settings)
    except Exception as e:
        app.logger.error(f"Error in api_program_settings route: {str(e)}", exc_info=True)
        return jsonify({"error": "An error occurred while loading program settings."}), 500

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
        
        if 'UI Settings' in new_settings:
            if 'enable_user_system' in new_settings['UI Settings']:
                # Handle enabling/disabling user system
                pass  # You may want to add logic here to handle user data when disabling the system

        # Update the config with new settings
        for section, settings in new_settings.items():
            if section not in config:
                config[section] = {}
            if section == 'Content Sources':
                # Update the Content Sources in the config dictionary
                config['Content Sources'] = settings
            elif section == 'Scraping':
                # Ensure 'Scraping' section exists
                if 'Scraping' not in config:
                    config['Scraping'] = {}
                # Update Scraping settings, including uncached_content_handling
                config['Scraping'].update(settings)
            else:
                config[section].update(settings)
        
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
@user_required
@onboarding_required
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
                item['filled_by_file'] = item.get('filled_by_file', 'Unknown')  # Add this line
        elif queue_name == 'Sleeping':
            for item in items:
                item['wake_count'] = queue_manager.get_wake_count(item['id'])

    upgrading_queue = queue_contents.get('Upgrading', [])
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
    perform_database_migration()
    initialize_app()
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

@app.route('/api/stop_program', methods=['POST'])
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
        'min_size_gb': 0.01,
        'max_size_gb': ''
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

@app.route('/versions/rename', methods=['POST'])
def rename_version():
    data = request.json
    old_name = data.get('old_name')
    new_name = data.get('new_name')
    
    if not old_name or not new_name:
        return jsonify({'success': False, 'error': 'Missing old_name or new_name'}), 400

    config = load_config()
    if 'Scraping' in config and 'versions' in config['Scraping']:
        versions = config['Scraping']['versions']
        if old_name in versions:
            versions[new_name] = versions.pop(old_name)
            save_config(config)
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Version not found'}), 404
    else:
        return jsonify({'success': False, 'error': 'Scraping versions not found in config'}), 404

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
@admin_required
@onboarding_required
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
            
            # Fetch IMDB IDs and season/episode counts for each result
            for result in search_results:
                app.logger.debug(f"Processing result: {result}")
                details = get_details(result)
                app.logger.debug(f"Details for result: {details}")
                
                if details:
                    imdb_id = details.get('externalIds', {}).get('imdbId', 'N/A')
                    result['imdbId'] = imdb_id
                    app.logger.debug(f"IMDB ID found: {imdb_id}")
                    
                    if result['mediaType'] == 'tv':
                        overseerr_url = get_setting('Overseerr', 'url')
                        overseerr_api_key = get_setting('Overseerr', 'api_key')
                        cookies = get_overseerr_cookies(overseerr_url)
                        season_episode_counts = get_all_season_episode_counts(overseerr_url, overseerr_api_key, result['id'], cookies)
                        result['seasonEpisodeCounts'] = season_episode_counts
                        app.logger.debug(f"Season/Episode counts: {season_episode_counts}")
                else:
                    result['imdbId'] = 'N/A'
                    app.logger.debug("No details found for this result")
            
            app.logger.debug(f"Final search results with IMDB IDs and season/episode counts: {search_results}")
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
def get_scraping_versions_route():
    try:
        config = load_config()
        versions = config.get('Scraping', {}).get('versions', {}).keys()
        return jsonify({'versions': list(versions)})
    except Exception as e:
        app.logger.error(f"Error getting scraping versions: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    
@app.route('/get_content_source_settings', methods=['GET'])
def get_content_source_settings_route():
    try:
        content_source_settings = get_content_source_settings()
        return jsonify(content_source_settings)
    except Exception as e:
        app.logger.error(f"Error getting content source settings: {str(e)}", exc_info=True)
        return jsonify({
            'error': str(e),
            'traceback': traceback.format_exc()
        }), 500

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

@app.route('/delete_database', methods=['POST'])
@admin_required
def delete_database():
    confirm_delete = request.form.get('confirm_delete')
    if confirm_delete != 'DELETE':
        return jsonify({'success': False, 'error': 'Invalid confirmation'})

    try:
        # Close any open database connections
        db.session.close()
        db.engine.dispose()

        # Delete the media_items.db file
        db_path = os.path.join(app.root_path, 'db_content', 'media_items.db')
        if os.path.exists(db_path):
            os.remove(db_path)
            logging.info(f"Deleted media_items.db file: {db_path}")
        else:
            logging.info(f"media_items.db file not found: {db_path}")

        # Recreate the tables
        create_tables()
        verify_database()

        return jsonify({'success': True, 'message': 'Database deleted and tables recreated successfully'})
    except Exception as e:
        logging.error(f"Error deleting database: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': f'An error occurred: {str(e)}'})


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
        genres = data.get('genres', [])
        
        if media_type == 'episode':
            season = int(data.get('season', 1))  # Convert to int, default to 1
            episode = int(data.get('episode', 1))  # Convert to int, default to 1
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
            imdb_id, tmdb_id, title, year, media_type, version, season, episode, multi, genres
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
                imdb_id, tmdb_id, title, year, media_type, version, season, episode, multi, genres
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
    global program_runner
    is_running = program_runner.is_running() if program_runner else False
    return jsonify({"running": is_running})

@app.route('/debug_functions')
def debug_functions():
    content_sources = get_all_settings().get('Content Sources', {})
    enabled_sources = {source: data for source, data in content_sources.items() if data.get('enabled', False)}
    return render_template('debug_functions.html', content_sources=enabled_sources)

@app.route('/bulk_delete_by_imdb', methods=['POST'])
def bulk_delete_by_imdb():
    imdb_id = request.form.get('imdb_id')
    if not imdb_id:
        return jsonify({'success': False, 'error': 'IMDB ID is required'})

    deleted_count = bulk_delete_by_imdb_id(imdb_id)
    if deleted_count > 0:
        return jsonify({'success': True, 'message': f'Successfully deleted {deleted_count} items with IMDB ID: {imdb_id}'})
    else:
        return jsonify({'success': False, 'error': f'No items found with IMDB ID: {imdb_id}'})

# Add this route to handle unauthorized access
@app.route('/unauthorized')
def unauthorized():
    flash('You are not authorized to access this page.', 'error')
    return redirect(url_for('login'))

def summarize_api_calls(time_frame):
    log_path = 'logs/api_calls.log'
    summary = defaultdict(lambda: defaultdict(int))
    
    with open(log_path, 'r') as f:
        for line in f:
            match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - API Call: (\w+) (.*) - Domain: (.*)', line)
            if match:
                timestamp, method, url, domain = match.groups()
                dt = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S,%f')
                
                if time_frame == 'hour':
                    key = dt.strftime('%Y-%m-%d %H:00')
                elif time_frame == 'day':
                    key = dt.strftime('%Y-%m-%d')
                elif time_frame == 'month':
                    key = dt.strftime('%Y-%m')
                
                summary[key][domain] += 1
    
    return dict(summary)

def update_cache_with_new_entries():
    global cache
    log_path = 'logs/api_calls.log'
    last_processed_line = cache['last_processed_line']
    
    with open(log_path, 'r') as f:
        lines = f.readlines()[last_processed_line:]
        
    for i, line in enumerate(lines, start=last_processed_line):
        match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - API Call: (\w+) (.*) - Domain: (.*)', line)
        if match:
            timestamp, method, url, domain = match.groups()
            dt = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S,%f')
            
            hour_key = dt.strftime('%Y-%m-%d %H:00')
            day_key = dt.strftime('%Y-%m-%d')
            month_key = dt.strftime('%Y-%m')
            
            for time_frame, key in [('hour', hour_key), ('day', day_key), ('month', month_key)]:
                if key not in cache[time_frame]:
                    cache[time_frame][key] = {}
                if domain not in cache[time_frame][key]:
                    cache[time_frame][key][domain] = 0
                cache[time_frame][key][domain] += 1
    
    cache['last_processed_line'] = last_processed_line + len(lines)
    save_cache(cache)

@app.route('/api_call_summary')
def api_call_summary():
    update_cache_with_new_entries()
    
    time_frame = request.args.get('time_frame', 'day')
    if time_frame not in ['hour', 'day', 'month']:
        time_frame = 'day'
    
    summary = cache[time_frame]
    
    # Get a sorted list of all domains
    all_domains = sorted(set(domain for period in summary.values() for domain in period))
    
    return render_template('api_call_summary.html', 
                           summary=summary, 
                           time_frame=time_frame,
                           all_domains=all_domains)

@app.route('/realtime_api_calls')
def realtime_api_calls():
    filter_domain = request.args.get('filter', '')
    latest_calls = get_latest_api_calls()
    
    if filter_domain:
        filtered_calls = [call for call in latest_calls if call['domain'] == filter_domain]
    else:
        filtered_calls = latest_calls
    
    all_domains = sorted(set(call['domain'] for call in latest_calls))
    
    return render_template('realtime_api_calls.html', 
                           calls=filtered_calls, 
                           filter=filter_domain,
                           all_domains=all_domains)

@app.route('/api/latest_calls')
def api_latest_calls():
    filter_domain = request.args.get('filter', '')
    latest_calls = get_latest_api_calls()
    
    if filter_domain:
        filtered_calls = [call for call in latest_calls if call['domain'] == filter_domain]
    else:
        filtered_calls = latest_calls
    
    return jsonify(filtered_calls)



@app.route('/clear_api_summary_cache', methods=['POST'])
@admin_required
def clear_api_summary_cache():
    global cache
    cache = {'hour': {}, 'day': {}, 'month': {}, 'last_processed_line': 0}
    save_cache(cache)
    return jsonify({"status": "success", "message": "API summary cache cleared"})

def get_latest_api_calls(limit=100):
    calls = []
    with open('logs/api_calls.log', 'r') as log_file:
        for line in reversed(list(log_file)):
            parts = line.strip().split(' - ', 1)
            if len(parts) == 2:
                timestamp, message = parts
                call_info = message.split(': ', 1)
                if len(call_info) == 2:
                    method_and_url = call_info[1].split(' ', 1)
                    if len(method_and_url) == 2:
                        method, url = method_and_url
                        domain = url.split('/')[2] if url.startswith('http') else 'unknown'
                        calls.append({
                            'timestamp': timestamp,
                            'method': method,
                            'url': url,
                            'domain': domain,
                            'endpoint': '/'.join(url.split('/')[3:]) if url.startswith('http') else url,
                            'status_code': 'N/A'
                        })
                        if len(calls) >= limit:
                            break
    return calls

@app.route('/api/get_collected_from_plex', methods=['POST'])
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

@app.route('/api/get_wanted_content', methods=['POST'])
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


if __name__ == '__main__':
    start_server()
