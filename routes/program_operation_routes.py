from flask import jsonify, request, current_app, Blueprint, logging, render_template
from routes import admin_required
from .database_routes import perform_database_migration 
from extensions import initialize_app 
from config_manager import load_config 
from settings import get_setting
import threading
from run_program import ProgramRunner
from flask_login import login_required
from requests.exceptions import RequestException
from api_tracker import api
import logging
import time
import socket
import os
import re

program_operation_bp = Blueprint('program_operation', __name__)

program_runner = None

def run_server():
    from extensions import app
    
    # Get port from environment variable or use default
    port = int(os.environ.get('CLI_DEBRID_PORT', 5000))
    app.run(debug=True, use_reloader=False, host='0.0.0.0', port=port)

def start_server():
    from extensions import app
    import socket
    
    # Get port from environment variable or use default
    port = int(os.environ.get('CLI_DEBRID_PORT', 5000))
    
    # Check if port is available
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('0.0.0.0', port))
        sock.close()
    except socket.error:
        logging.error(f"Port {port} is already in use. Please close any other instances or applications using this port.")
        return False
        
    with app.app_context():
        perform_database_migration()
        initialize_app()
    server_thread = threading.Thread(target=run_server)
    server_thread.daemon = True
    server_thread.start()
    return True

def check_service_connectivity():
    if get_setting('File Management', 'file_collection_management') == 'Plex':
        plex_url = get_setting('Plex', 'url')
        plex_token = get_setting('Plex', 'token')
    metadata_battery_url = get_setting('Metadata Battery', 'url')
    battery_port = int(os.environ.get('CLI_DEBRID_BATTERY_PORT', 5001))
    
    # Get debrid provider settings
    debrid_provider = get_setting('Debrid Provider', 'provider')
    debrid_api_key = get_setting('Debrid Provider', 'api_key')

    services_reachable = True

    # Check Plex connectivity
    if get_setting('File Management', 'file_collection_management') == 'Plex':
        try:
            response = api.get(f"{plex_url}?X-Plex-Token={plex_token}", timeout=5)
            response.raise_for_status()
        except RequestException as e:
            logging.error(f"Failed to connect to Plex server: {str(e)}")
            services_reachable = False

    # Check Debrid Provider connectivity
    if debrid_provider.lower() == 'realdebrid':
        try:
            response = api.get("https://api.real-debrid.com/rest/1.0/user", headers={"Authorization": f"Bearer {debrid_api_key}"}, timeout=5)
            response.raise_for_status()
        except RequestException as e:
            logging.error(f"Failed to connect to Real-Debrid API: {str(e)}")
            services_reachable = False
    elif debrid_provider.lower() == 'torbox':
        try:
            response = api.get("https://torbox.app/api/v1/user", headers={"Authorization": f"Bearer {debrid_api_key}"}, timeout=5)
            response.raise_for_status()
        except RequestException as e:
            logging.error(f"Failed to connect to Torbox API: {str(e)}")
            services_reachable = False
    else:
        logging.error(f"Unknown debrid provider: {debrid_provider}")
        services_reachable = False

    # Check Metadata Battery connectivity and Trakt authorization
    try:
        # Remove any trailing port numbers and slashes
        metadata_battery_url = re.sub(r':\d+/?$', '', metadata_battery_url)
        
        # Use the configured battery port
        metadata_battery_url += f':{battery_port}'
        response = api.get(f"{metadata_battery_url}/check_trakt_auth", timeout=5)
        response.raise_for_status()
        trakt_status = response.json().get('status')
        if trakt_status != 'authorized':
            logging.warning("Metadata Battery is reachable, but Trakt is not authorized.")
            services_reachable = False
    except RequestException as e:
        if hasattr(e, 'response') and e.response is not None:
            logging.error(f"Failed to connect to Metadata Battery: {e.response.status_code} {e.response.reason}")
            logging.error(f"Response content: {e.response.text}")
        else:
            logging.error(f"Failed to connect to Metadata Battery: {str(e)}")
        services_reachable = False

    return services_reachable

@program_operation_bp.route('/api/start_program', methods=['POST'])
def start_program():
    global program_runner
    if program_runner is None or not program_runner.is_running():
        # Add delay if auto-start is enabled
        if get_setting('Debug', 'auto_run_program', default=False):
            time.sleep(1)  # 1 second delay for auto-start

        # Check service connectivity before starting the program
        if not check_service_connectivity():
            return jsonify({"status": "error", "message": "Failed to connect to Plex, Debrid Provider, or Metadata Battery. Check logs for details."})

        program_runner = ProgramRunner()
        # Start the program runner in a separate thread to avoid blocking the Flask server
        threading.Thread(target=program_runner.start).start()
        current_app.config['PROGRAM_RUNNING'] = True
        return jsonify({"status": "success", "message": "Program started"})
    else:
        return jsonify({"status": "error", "message": "Program is already running"})

def stop_program():
    global program_runner
    if program_runner is not None and program_runner.is_running():
        program_runner.stop()
        program_runner = None
        current_app.config['PROGRAM_RUNNING'] = False
        return {"status": "success", "message": "Program stopped"}
    else:
        return {"status": "error", "message": "Program is not running"}

@program_operation_bp.route('/api/stop_program', methods=['POST'])
def stop_program_route():
    result = stop_program()
    return jsonify(result)

@program_operation_bp.route('/api/update_program_state', methods=['POST'])
def update_program_state():
    state = request.json.get('state')
    if state in ['Running', 'Initialized']:
        current_app.config['PROGRAM_RUNNING'] = (state == 'Running')
        return jsonify({"status": "success", "message": f"Program state updated to {state}"})
    else:
        return jsonify({"status": "error", "message": "Invalid state"}), 400

@program_operation_bp.route('/api/program_status', methods=['GET'])
def program_status():
    global program_runner
    is_running = program_runner.is_running() if program_runner else False
    is_initializing = program_runner.is_initializing() if program_runner else False
    return jsonify({"running": is_running, "initializing": is_initializing})

def program_is_running():
    global program_runner
    return program_runner.is_running() if program_runner else False

def program_is_initializing():  # Add this function
    global program_runner
    return program_runner.is_initializing() if program_runner else False

@program_operation_bp.route('/api/check_program_conditions')
@login_required
@admin_required
def check_program_conditions():
    config = load_config()
    scrapers_enabled = any(scraper.get('enabled', False) for scraper in config.get('Scrapers', {}).values())
    content_sources_enabled = any(source.get('enabled', False) for source in config.get('Content Sources', {}).values())
    
    required_settings = [
        ('Plex', 'url'),
        ('Plex', 'token'),
        ('Debrid Provider', 'provider'),
        ('Debrid Provider', 'api_key'),
        ('Metadata Battery', 'url')
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

@program_operation_bp.route('/api/task_timings', methods=['GET'])
@login_required
def get_task_timings():
    global program_runner
    
    if not program_runner or not program_runner.is_running():
        return jsonify({
            "status": "error",
            "message": "Program is not running"
        }), 404

    current_time = time.time()
    task_timings = {}
    
    # Get all task intervals and their last run times
    for task, interval in program_runner.task_intervals.items():
        last_run = program_runner.last_run_times.get(task, current_time)
        time_until_next_run = interval - (current_time - last_run)
        
        # Convert to hours, minutes, seconds
        hours, remainder = divmod(int(time_until_next_run), 3600)
        minutes, seconds = divmod(remainder, 60)
        
        task_timings[task] = {
            "next_run_in": {
                "hours": hours,
                "minutes": minutes,
                "seconds": seconds,
                "total_seconds": time_until_next_run
            },
            "interval": interval,
            "last_run": last_run,
            "enabled": task in program_runner.enabled_tasks
        }

    # Group tasks by type
    grouped_timings = {
        "queues": {},
        "content_sources": {},
        "system_tasks": {}
    }

    for task, timing in task_timings.items():
        if task in ['Wanted', 'Scraping', 'Adding', 'Checking', 'Sleeping', 
                   'Unreleased', 'Blacklisted', 'Pending Uncached', 'Upgrading']:
            grouped_timings["queues"][task] = timing
        elif task.endswith('_wanted'):
            grouped_timings["content_sources"][task] = timing
        else:
            grouped_timings["system_tasks"][task] = timing

    return jsonify({
        "status": "success",
        "data": grouped_timings,
        "current_time": current_time
    })

@program_operation_bp.route('/task_timings')
@login_required
@admin_required
def task_timings():
    return render_template('task_timings.html')
