from flask import Flask, redirect, url_for, send_from_directory
from flask_session import Session
import time
from queue_manager import QueueManager
import logging
import os
from datetime import datetime
from template_utils import render_settings, render_content_sources
import json
from routes.program_operation_routes import program_is_running, start_server
from sqlalchemy import inspect
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from extensions import db, app, app_start_time
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
    return send_from_directory(os.path.join(app.root_path, 'static'),
                               'favicon.ico', mimetype='image/vnd.microsoft.icon')

@app.route('/site.webmanifest')
def manifest():
    manifest_path = os.path.join(app.static_folder, 'site.webmanifest')
    if not os.path.exists(manifest_path):
        return "Manifest file not found", 404
    
    try:
        response = send_from_directory(app.static_folder, 'site.webmanifest', mimetype='application/manifest+json')
        return response
    except Exception as e:
        return f"Error serving manifest: {str(e)}", 500

if __name__ == '__main__':
    start_server()