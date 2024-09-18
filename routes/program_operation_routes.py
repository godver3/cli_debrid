from flask import jsonify, request, current_app, Blueprint, logging
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

program_operation_bp = Blueprint('program_operation', __name__)

program_runner = None

def run_server():
    from extensions import app
   
    app.run(debug=True, use_reloader=False, host='0.0.0.0', port=5000)

def start_server():
    from extensions import app
    with app.app_context():
        perform_database_migration()
        initialize_app()
    server_thread = threading.Thread(target=run_server)
    server_thread.daemon = True
    server_thread.start()

def check_service_connectivity():
    plex_url = get_setting('Plex', 'url')
    plex_token = get_setting('Plex', 'token')
    rd_api_key = get_setting('RealDebrid', 'api_key')
    metadata_battery_url = get_setting('Metadata Battery', 'url')
    
    # Update this line in your settings to use:
    # metadata_battery_url = "http://cli_battery_app:5001"

    services_reachable = True

    # Check Plex connectivity
    try:
        response = api.get(f"{plex_url}?X-Plex-Token={plex_token}", timeout=5)
        response.raise_for_status()
        logging.debug("Plex server is reachable.")
    except RequestException as e:
        logging.error(f"Failed to connect to Plex server: {str(e)}")
        services_reachable = False

    # Check Real Debrid connectivity
    try:
        response = api.get("https://api.real-debrid.com/rest/1.0/user", headers={"Authorization": f"Bearer {rd_api_key}"}, timeout=5)
        response.raise_for_status()
        logging.debug("Real Debrid API is reachable.")
    except RequestException as e:
        logging.error(f"Failed to connect to Real Debrid API: {str(e)}")
        services_reachable = False

    # Check Metadata Battery connectivity and Trakt authorization
    try:
        # Remove trailing ":5000" or ":5000/" if present
        metadata_battery_url = metadata_battery_url.rstrip('/').removesuffix(':5001')
        metadata_battery_url = metadata_battery_url.rstrip('/').removesuffix(':50051')
        
        # Append ":50051"
        metadata_battery_url += ':5001'
        response = api.get(f"{metadata_battery_url}/check_trakt_auth", timeout=5)
        response.raise_for_status()
        trakt_status = response.json().get('status')
        if trakt_status == 'authorized':
            logging.debug("Metadata Battery is reachable and authorized with Trakt.")
        else:
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
        # Check service connectivity before starting the program
        if not check_service_connectivity():
            return jsonify({"status": "error", "message": "Failed to connect to Plex, Real Debrid, or Metadata Battery. Check logs for details."})

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
        ('RealDebrid', 'api_key'),
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
