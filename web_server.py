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
import logging

from routes import register_blueprints, auth_bp

app.config['SESSION_TYPE'] = 'filesystem'
Session(app)
app.secret_key = '9683650475'

queue_manager = QueueManager()

db_directory = os.path.join(app.root_path, 'user/db_content')
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

CONFIG_FILE = './config/config.json'

register_blueprints(app)

@app.context_processor
def inject_program_status():
    return dict(program_is_running=program_is_running)

@app.context_processor
def utility_processor():
    from routes.settings_routes import is_user_system_enabled
    return dict(render_settings=render_settings, render_content_sources=render_content_sources, is_user_system_enabled=is_user_system_enabled)

@app.context_processor
def inject_version():
    try:
        with open('version.txt', 'r') as f:
            version = f.read().strip()
    except FileNotFoundError:
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

@app.route('/')
def index():
    from routes.settings_routes import is_user_system_enabled
    logging.debug("Entering index route")
    if not is_user_system_enabled() or current_user.is_authenticated:
        logging.debug("Redirecting to statistics.index")
        return redirect(url_for('statistics.index'))
    else:
        logging.debug("Redirecting to auth.login")
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