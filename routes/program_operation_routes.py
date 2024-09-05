from flask import jsonify, request, current_app, Blueprint
from routes import admin_required
from .database_routes import perform_database_migration 
from extensions import initialize_app 
from config_manager import load_config 
from settings import get_setting
import threading
from run_program import ProgramRunner
from flask_login import login_required

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

@program_operation_bp.route('/api/start_program', methods=['POST'])
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

@program_operation_bp.route('/api/stop_program', methods=['POST'])
def reset_program():
    global program_runner
    if program_runner is not None:
        program_runner.stop()
    program_runner = None
    current_app.config['PROGRAM_RUNNING'] = False
    return jsonify({"status": "success", "message": "Program reset"})

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
    return jsonify({"running": is_running})

def program_is_running():
    global program_runner
    return program_runner.is_running() if program_runner else False

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
