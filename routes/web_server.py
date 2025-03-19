from flask import Flask, redirect, url_for, send_from_directory
from flask_session import Session
import time
from queues.queue_manager import QueueManager
import logging
import os
from datetime import datetime
from routes.template_utils import render_settings, render_content_sources
import json
from routes.program_operation_routes import program_is_running, start_server
from sqlalchemy import inspect
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from routes.extensions import db, app, app_start_time
from routes.auth_routes import init_db
from flask_login import current_user
import sys

from routes import register_blueprints, auth_bp

# Get db_content directory from environment variable with fallback
db_directory = os.environ.get('USER_DB_CONTENT', '/user/db_content')
os.makedirs(db_directory, exist_ok=True)

if not os.access(db_directory, os.W_OK):
    raise PermissionError(f"The directory {db_directory} is not writable. Please check permissions.")

db_path = os.path.join(db_directory, 'users.db')
app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{db_path}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

init_db(app)

# Disable Werkzeug request logging
log = logging.getLogger('werkzeug')
log.disabled = True

# Configure logging for web_server
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

# Global variables for statistics
start_time = time.time()

# Get config directory from environment variable with fallback
config_dir = os.environ.get('USER_CONFIG', '/user/config')
CONFIG_FILE = os.path.join(config_dir, 'runtime-config.json')

register_blueprints(app)

@app.context_processor
def inject_program_status():
    return dict(program_is_running=program_is_running)

@app.context_processor
def inject_logo_selection():
    from utilities.settings import get_setting
    # Get the logo selection from settings
    logo_selection = get_setting('UI Settings', 'program_logo', 'Default')
    
    # Define logo mappings based on selection
    logo_paths = {
        'Default': 'white-icon-32x32.png',  # Default is the white icon
        'Plex': 'plex-icon-32x32.png'
    }
    
    # Get the appropriate logo path or default to white icon if selection not found
    logo_path = logo_paths.get(logo_selection, 'white-icon-32x32.png')
    
    return dict(logo_path=logo_path)

@app.context_processor
def inject_support_message_setting():
    from utilities.settings import get_setting
    # Get the hide_support_message setting from UI Settings
    hide_support_message = get_setting('UI Settings', 'hide_support_message', False)
    return dict(hide_support_message=hide_support_message)

@app.context_processor
def utility_processor():
    from routes.settings_routes import is_user_system_enabled
    return dict(render_settings=render_settings, 
                render_content_sources=render_content_sources, 
                is_user_system_enabled=is_user_system_enabled)

@app.context_processor
def inject_version():
    try:
        # Get the application's root directory
        if getattr(sys, 'frozen', False):
            # If frozen (exe), look in the PyInstaller temp directory
            base_dir = os.path.dirname(__file__)
        else:
            # If running from source, use the directory containing this script
            base_dir = os.path.dirname(os.path.abspath(__file__))
        
        version_path = os.path.join(base_dir, 'version.txt')
        
        with open(version_path, 'r') as f:
            version = f.read().strip()
    except FileNotFoundError:
        version = "Unknown"
    except Exception as e:
        version = "Unknown"
    return dict(version=version)

@app.template_filter('isinstance')
def isinstance_filter(value, class_name):
    return isinstance(value, getattr(datetime, class_name, type(None)))

@app.template_filter('is_infinite')
def is_infinite(value):
    return value == float('inf')

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

@app.template_filter('zfill')
def zfill_filter(value, width=None):
    """Pad a numeric string with zeros."""
    if value is None:
        return ""
    return str(value).zfill(width if width is not None else 2)

@app.route('/')
def index():
    from routes.settings_routes import is_user_system_enabled
    if not is_user_system_enabled() or current_user.is_authenticated:
        return redirect(url_for('root.root'))
    else:
        return redirect(url_for('auth.login'))

@app.route('/favicon.ico')
def favicon():
    from utilities.settings import get_setting
    
    # Get the logo selection from settings
    logo_selection = get_setting('UI Settings', 'program_logo', 'Default')
    
    # Define favicon mappings based on selection
    favicon_paths = {
        'Default': 'favicon.ico',  # Default favicon
        'Plex': 'plex-icon-32x32.ico'
    }
    
    # Get the appropriate favicon path or default to regular favicon if selection not found
    favicon_path = favicon_paths.get(logo_selection, 'favicon.ico')
    
    return send_from_directory(os.path.join(app.root_path, 'static'),
                               favicon_path, mimetype='image/vnd.microsoft.icon')

@app.route('/favicon-<size>.png')
def dynamic_favicon(size):
    from utilities.settings import get_setting
    
    # Get the logo selection from settings
    logo_selection = get_setting('UI Settings', 'program_logo', 'Default')
    
    # Define icon mappings based on selection and size
    icon_paths = {
        'Default': {
            '16x16': 'icon-16x16.png',
            '32x32': 'icon-32x32.png',
            '192x192': 'android-chrome-192x192.png',
            '512x512': 'android-chrome-512x512.png'
        },
        'Plex': {
            '16x16': 'plex-icon-16x16.png',
            '32x32': 'plex-icon-32x32.png',
            '192x192': 'plex-favicon-192x192.png',
            '512x512': 'plex-favicon-512x512.png'
        }
    }
    
    # Get the appropriate icon path or default to regular icon if selection not found
    icon_path = icon_paths.get(logo_selection, icon_paths['Default']).get(size, f'favicon-{size}.png')
    
    return send_from_directory(os.path.join(app.root_path, 'static'),
                               icon_path, mimetype='image/png')

@app.route('/site.webmanifest')
def manifest():
    from utilities.settings import get_setting
    from flask import jsonify
    
    # Get the logo selection from settings
    logo_selection = get_setting('UI Settings', 'program_logo', 'Default')
    
    # Choose icons based on logo selection
    if logo_selection == 'Plex':
        icons = [
            {
                "src": "/static/plex-favicon-192x192.png",
                "sizes": "192x192",
                "type": "image/png"
            },
            {
                "src": "/static/plex-favicon-512x512.png",
                "sizes": "512x512",
                "type": "image/png"
            }
        ]
    else:
        icons = [
            {
                "src": "/static/android-chrome-192x192.png",
                "sizes": "192x192",
                "type": "image/png"
            },
            {
                "src": "/static/android-chrome-512x512.png",
                "sizes": "512x512",
                "type": "image/png"
            }
        ]
    
    # Create the manifest JSON
    manifest_data = {
        "name": "cli_debrid",
        "short_name": "cli_debrid",
        "icons": icons,
        "theme_color": "#007bff",
        "background_color": "#ffffff",
        "display": "standalone"
    }
    
    response = jsonify(manifest_data)
    response.headers['Content-Type'] = 'application/manifest+json'
    return response

if __name__ == '__main__':
    start_server()