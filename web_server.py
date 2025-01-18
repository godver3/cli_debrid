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

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logging.debug("[login_testing] Starting server initialization")

# Initialize database
db_directory = os.environ.get('USER_DB_CONTENT', '/user/db_content')
os.makedirs(db_directory, exist_ok=True)
logging.debug("[login_testing] Database directory: %s", db_directory)

db_path = os.path.join(db_directory, 'users.db')
logging.debug("[login_testing] Database path: %s", db_path)
app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{db_path}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

logging.debug("[login_testing] Initializing database")
init_db(app)
logging.debug("[login_testing] Database initialization complete")

# Register blueprints and routes
logging.debug("[login_testing] Registering blueprints")
register_blueprints(app)
logging.debug("[login_testing] Blueprints registered")

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
        base_dir = os.path.dirname(os.path.abspath(__file__)) if not getattr(sys, 'frozen', False) else os.path.dirname(__file__)
        version_path = os.path.join(base_dir, 'version.txt')
        
        with open(version_path, 'r') as f:
            version = f.read().strip()
    except Exception:
        version = "Unknown"
    return dict(version=version)

@app.route('/')
def index():
    logging.debug("[login_testing] Processing index route")
    from routes.settings_routes import is_user_system_enabled
    if not is_user_system_enabled() or current_user.is_authenticated:
        logging.debug("[login_testing] Redirecting to root.root - User system enabled: %s, User authenticated: %s",
                     is_user_system_enabled(), current_user.is_authenticated)
        return redirect(url_for('root.root'))
    logging.debug("[login_testing] Redirecting to login")
    return redirect(url_for('auth.login'))

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'),
                               'favicon.ico', mimetype='image/vnd.microsoft.icon')

@app.route('/site.webmanifest')
def manifest():
    return send_from_directory(app.static_folder, 'site.webmanifest', 
                             mimetype='application/manifest+json')

if __name__ == '__main__':
    start_server()