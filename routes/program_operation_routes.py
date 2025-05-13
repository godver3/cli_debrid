from flask import jsonify, request, current_app, Blueprint, logging, render_template
from routes import admin_required, user_required
from .database_routes import perform_database_migration 
from routes.extensions import initialize_app 
from queues.config_manager import load_config 
from utilities.settings import get_setting
import threading
from queues.run_program import ProgramRunner, _setup_scheduler_listeners
from flask_login import login_required
from requests.exceptions import RequestException
from routes.api_tracker import api
import logging
import time
import socket
import os
import re
import signal
import psutil
import sys
import subprocess
import xml.etree.ElementTree as ET
from routes.notifications import (
    send_queue_start_notification,
    send_queue_stop_notification
)
from apscheduler.triggers.interval import IntervalTrigger
from datetime import timezone
import pytz
from datetime import datetime, timedelta
import json

program_operation_bp = Blueprint('program_operation', __name__)

program_runner = None
server_thread = None

def get_program_runner():
    global program_runner
    return program_runner

def cleanup_port(port):
    """Cleanup any process using the specified port using multiple methods."""
    try:
        success = False
        
        # Method 1: Try lsof
        try:
            output = subprocess.check_output(['lsof', '-t', '-i', f':{port}'], stderr=subprocess.PIPE)
            pids = output.decode().strip().split('\n')
            
            for pid in pids:
                if pid:  # Check if pid is not empty
                    pid = int(pid)
                    logging.info(f"Found process {pid} using port {port} via lsof")
                    try:
                        # First try SIGTERM
                        os.kill(pid, signal.SIGTERM)
                        logging.info(f"Sent SIGTERM to process {pid}")
                        
                        # Wait up to 5 seconds for process to terminate
                        for _ in range(50):  # 50 * 0.1 = 5 seconds
                            try:
                                # Check if process still exists
                                os.kill(pid, 0)
                                time.sleep(0.1)
                            except OSError:
                                # Process has terminated
                                success = True
                                break
                        
                        # If process still exists after timeout, use SIGKILL
                        try:
                            os.kill(pid, 0)
                            logging.warning(f"Process {pid} did not respond to SIGTERM, using SIGKILL")
                            os.kill(pid, signal.SIGKILL)
                        except OSError:
                            # Process has already terminated
                            pass
                            
                        success = True
                    except OSError as e:
                        logging.error(f"Error killing process {pid}: {e}")
                        
        except subprocess.CalledProcessError as e:
            if e.returncode == 1 and not e.output:  # lsof returns 1 when no processes found
                logging.info(f"No processes found using port {port} via lsof")
            else:
                logging.error(f"Error running lsof: {e}")
        
        # Method 2: Try netstat
        try:
            output = subprocess.check_output(['netstat', '-tlpn'], stderr=subprocess.PIPE)
            for line in output.decode().split('\n'):
                if f':{port}' in line:
                    # Extract PID from netstat output
                    match = re.search(r'LISTEN\s+(\d+)/', line)
                    if match:
                        pid = int(match.group(1))
                        logging.info(f"Found process {pid} using port {port} via netstat")
                        try:
                            # First try SIGTERM
                            os.kill(pid, signal.SIGTERM)
                            logging.info(f"Sent SIGTERM to process {pid}")
                            
                            # Wait up to 5 seconds for process to terminate
                            for _ in range(50):  # 50 * 0.1 = 5 seconds
                                try:
                                    # Check if process still exists
                                    os.kill(pid, 0)
                                    time.sleep(0.1)
                                except OSError:
                                    # Process has terminated
                                    success = True
                                    break
                            
                            # If process still exists after timeout, use SIGKILL
                            try:
                                os.kill(pid, 0)
                                logging.warning(f"Process {pid} did not respond to SIGTERM, using SIGKILL")
                                os.kill(pid, signal.SIGKILL)
                            except OSError:
                                # Process has already terminated
                                pass
                                
                            success = True
                        except OSError as e:
                            logging.error(f"Error killing process {pid}: {e}")
        except subprocess.CalledProcessError as e:
            logging.error(f"Error running netstat: {e}")
        
        # Method 3: Try fuser as last resort
        try:
            output = subprocess.check_output(['fuser', f'{port}/tcp'], stderr=subprocess.PIPE)
            pids = output.decode().strip().split()
            for pid in pids:
                if pid:
                    pid = int(pid)
                    logging.info(f"Found process {pid} using port {port} via fuser")
                    try:
                        # First try SIGTERM
                        os.kill(pid, signal.SIGTERM)
                        logging.info(f"Sent SIGTERM to process {pid}")
                        
                        # Wait up to 5 seconds for process to terminate
                        for _ in range(50):  # 50 * 0.1 = 5 seconds
                            try:
                                # Check if process still exists
                                os.kill(pid, 0)
                                time.sleep(0.1)
                            except OSError:
                                # Process has terminated
                                success = True
                                break
                        
                        # If process still exists after timeout, use SIGKILL
                        try:
                            os.kill(pid, 0)
                            logging.warning(f"Process {pid} did not respond to SIGTERM, using SIGKILL")
                            os.kill(pid, signal.SIGKILL)
                        except OSError:
                            # Process has already terminated
                            pass
                            
                        success = True
                    except OSError as e:
                        logging.error(f"Error killing process {pid}: {e}")
        except subprocess.CalledProcessError as e:
            if e.returncode == 1:  # fuser returns 1 when no processes found
                logging.info(f"No processes found using port {port} via fuser")
            else:
                logging.error(f"Error running fuser: {e}")
        
        if success:
            # Give the system time to fully release the port
            time.sleep(2)
            return True
            
        return False
            
    except Exception as e:
        logging.error(f"Error cleaning up port {port}: {str(e)}")
        return False

def signal_handler(signum, frame):
    """Handle termination signals gracefully."""
    logging.info(f"Received signal {signum}")
    stop_program()
    sys.exit(0)

def run_server():
    from routes.extensions import app
    
    # Get port from environment variable or use default
    port = int(os.environ.get('CLI_DEBRID_PORT', 5000))
    try:
        app.run(debug=True, use_reloader=False, host='0.0.0.0', port=port)
    except Exception as e:
        logging.error(f"Error running server: {str(e)}")
        cleanup_port(port)

def start_server():
    from routes.extensions import app
    import socket
    
    # Get port from environment variable or use default
    port = int(os.environ.get('CLI_DEBRID_PORT', 5000))
    max_retries = 3
    retry_delay = 2  # seconds
    
    for attempt in range(max_retries):
        # Check if port is available
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(('0.0.0.0', port))
            sock.close()
            
            if result != 0:  # Port is free
                break
                
            # Port is in use, try to clean it up
            logging.warning(f"Port {port} is in use (attempt {attempt + 1}/{max_retries}), attempting to clean up...")
            if cleanup_port(port):
                time.sleep(retry_delay)  # Wait for port to be fully released
                continue
            else:
                if attempt == max_retries - 1:
                    logging.error(f"Port {port} is still in use after {max_retries} cleanup attempts. Please try again or use a different port.")
                    return False
        except Exception as e:
            logging.error(f"Error checking port {port}: {e}")
            return False
        
    # Setup signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
        
    with app.app_context():
        perform_database_migration()
        initialize_app()
    
    global server_thread
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
    failed_services_details = []

    # Check Symlink paths if using symlink management
    if get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
        original_path = get_setting('File Management', 'original_files_path')
        symlinked_path = get_setting('File Management', 'symlinked_files_path')
        
        # Check original files path
        if not os.path.exists(original_path):
            logging.error(f"Cannot access original files path: {original_path}")
            services_reachable = False
            failed_services_details.append({"service": f"Original files path ({original_path})", "type": "CONFIG_ERROR", "message": "Cannot access original files path."})
        elif not os.listdir(original_path):
            logging.warning(f"Original files path is empty: {original_path}")
            
        # Check symlinked files path
        if not os.path.exists(symlinked_path):
            try:
                os.makedirs(symlinked_path, exist_ok=True)
                logging.info(f"Created symlinked files path: {symlinked_path}")
            except Exception as e:
                logging.error(f"Cannot create symlinked files path: {symlinked_path}. Error: {str(e)}")
                services_reachable = False
                failed_services_details.append({"service": f"Symlinked files path ({symlinked_path})", "type": "CONFIG_ERROR", "message": f"Cannot create symlinked files path. Error: {str(e)}"})
                
        # Check Plex connectivity for symlink updates if enabled
        plex_url_symlink = get_setting('File Management', 'plex_url_for_symlink')
        plex_token_symlink = get_setting('File Management', 'plex_token_for_symlink')
        
        if plex_url_symlink and plex_token_symlink:
            try:
                response = api.get(f"{plex_url_symlink}?X-Plex-Token={plex_token_symlink}", timeout=5)
                response.raise_for_status()
                
                # Verify we got a valid Plex response by checking for required attributes
                root = ET.fromstring(response.text)
                if not root.get('machineIdentifier') or not root.get('myPlexSigninState'):
                    error_msg = "Invalid Plex response for symlink updates - token may be incorrect"
                    logging.error(error_msg)
                    services_reachable = False
                    failed_services_details.append({"service": "Plex (for symlink updates)", "type": "INVALID_TOKEN", "message": error_msg})
                else:
                    services_reachable = True  # Set to True when Plex is reachable
                    logging.debug("Plex connectivity check passed")
            except (RequestException, ET.ParseError) as e:
                error_msg = f"Cannot connect to Plex server for symlink updates. Error: {str(e)}"
                logging.error(error_msg)
                services_reachable = False
                failed_services_details.append({"service": "Plex (for symlink updates)", "type": "CONNECTION_ERROR", "status_code": None, "message": error_msg})

    # Check Plex connectivity and libraries
    if get_setting('File Management', 'file_collection_management') == 'Plex':
        try:
            # First check basic connectivity and token validity
            response = api.get(f"{plex_url}?X-Plex-Token={plex_token}", timeout=5)
            response.raise_for_status()
            
            # Verify we got a valid Plex response by checking for required attributes
            try:
                root = ET.fromstring(response.text)
                if not root.get('machineIdentifier') or not root.get('myPlexSigninState'):
                    error_msg = "Invalid Plex response - token may be incorrect"
                    logging.error(error_msg)
                    services_reachable = False
                    failed_services_details.append({"service": "Plex", "type": "INVALID_TOKEN", "message": error_msg})
                    return services_reachable, failed_services_details
                else:
                    logging.info(f"Successfully validated Plex connection (Server: {root.get('friendlyName', 'Unknown')})")
            except ET.ParseError as e:
                error_msg = f"Invalid Plex response format: {str(e)}"
                logging.error(error_msg)
                services_reachable = False
                failed_services_details.append({"service": "Plex", "type": "INVALID_RESPONSE_FORMAT", "message": error_msg})
                return services_reachable, failed_services_details

            # Then check library existence
            libraries_response = api.get(f"{plex_url}/library/sections?X-Plex-Token={plex_token}", timeout=5)
            libraries_response.raise_for_status()
            
            # Get configured library names
            movie_libraries = [lib.strip() for lib in get_setting('Plex', 'movie_libraries', '').split(',') if lib.strip()]
            show_libraries = [lib.strip() for lib in get_setting('Plex', 'shows_libraries', '').split(',') if lib.strip()]
            
            try:
                # Get actual library names from Plex (XML format)
                available_library_titles = []
                library_id_to_title = {}  # Map to store ID -> Title mapping
                root = ET.fromstring(libraries_response.text)
                for directory in root.findall('.//Directory'):
                    library_title = directory.get('title')
                    library_key = directory.get('key')
                    if library_title and library_key:
                        available_library_titles.append(library_title)
                        library_id_to_title[library_key] = library_title
                        logging.info(f"Found Plex library: ID={library_key}, Title='{library_title}', Type={directory.get('type')}")
                
                # Create a set of lowercase titles for efficient case-insensitive check
                available_library_titles_lower = {title.lower() for title in available_library_titles}

                if not available_library_titles:
                    logging.error("No libraries found in Plex response")
                    services_reachable = False
                    failed_services_details.append({"service": "Plex", "type": "NO_LIBRARIES_FOUND", "message": "No libraries found in Plex response."})
                    return services_reachable, failed_services_details

                # Verify all configured libraries exist (check IDs case-sensitively, names case-insensitively)
                missing_libraries = []
                for lib_name_or_id in movie_libraries + show_libraries:
                    # Check if it exists as an ID (case-sensitive) or as a name (case-insensitive)
                    if lib_name_or_id not in library_id_to_title and lib_name_or_id.lower() not in available_library_titles_lower:
                         missing_libraries.append(lib_name_or_id)
                         logging.warning(f"Configured library '{lib_name_or_id}' not found in available libraries (IDs: {list(library_id_to_title.keys())}, Names: {available_library_titles})")


                if missing_libraries:
                    error_msg = "Cannot start program: The following Plex libraries were not found:<ul>"
                    for lib in missing_libraries:
                        error_msg += f"<li>{lib}</li>"
                    error_msg += "</ul>Available libraries are:<ul>"
                    for title in available_library_titles:
                        error_msg += f"<li>{title}</li>"
                    error_msg += "</ul>Please verify your Plex library names in settings."
                    logging.error(error_msg)
                    services_reachable = False
                    failed_services_details.append({"service": f"Plex (missing libraries: {', '.join(missing_libraries)})", "type": "CONFIG_ERROR", "message": error_msg})
                    return services_reachable, failed_services_details

            except ET.ParseError as e:
                error_msg = f"Failed to parse Plex libraries response (XML): {str(e)}"
                logging.error(error_msg)
                services_reachable = False
                failed_services_details.append({"service": "Plex", "type": "INVALID_LIBRARIES_RESPONSE", "message": error_msg})
                return services_reachable, failed_services_details

        except RequestException as e:
            error_msg = f"Cannot start program: Failed to connect to Plex server. Error: {str(e)}"
            logging.error(error_msg)
            services_reachable = False
            failed_services_details.append({"service": "Plex", "type": "CONNECTION_ERROR", "status_code": None, "message": error_msg})

    # Check Debrid Provider connectivity
    if debrid_provider.lower() == 'realdebrid':
        try:
            response = api.get("https://api.real-debrid.com/rest/1.0/user", headers={"Authorization": f"Bearer {debrid_api_key}"}, timeout=5)
            response.raise_for_status()
        except RequestException as e:
            logging.error(f"Failed to connect to Real-Debrid API: {str(e)}")
            services_reachable = False
            error_detail = {"service": "Real-Debrid API", "type": "CONNECTION_ERROR", "status_code": None, "message": str(e)}
            if hasattr(e, 'response') and e.response is not None:
                error_detail["status_code"] = e.response.status_code
                if e.response.status_code == 401:
                    error_detail["type"] = "UNAUTHORIZED"
                    error_detail["message"] = "Real-Debrid API Key is invalid or unauthorized."
                elif e.response.status_code == 403: # Forbidden, could also be an API key issue or IP block
                    error_detail["type"] = "FORBIDDEN"
                    error_detail["message"] = "Real-Debrid API access forbidden. Check API key, IP, or account status."
                # Add other specific status code checks if needed
            failed_services_details.append(error_detail)
    else:
        logging.error(f"Unknown debrid provider: {debrid_provider}")
        services_reachable = False
        failed_services_details.append({"service": f"Unknown debrid provider ({debrid_provider})", "type": "CONFIG_ERROR", "message": "Invalid Debrid provider configured."})

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
            failed_services_details.append({"service": "Trakt", "type": "UNAUTHORIZED", "message": "Trakt not authorized via Metadata Battery."})
    except RequestException as e:
        if hasattr(e, 'response') and e.response is not None:
            logging.error(f"Failed to connect to Metadata Battery: {e.response.status_code} {e.response.reason}")
            logging.error(f"Response content: {e.response.text}")
        else:
            logging.error(f"Failed to connect to Metadata Battery: {str(e)}")
        services_reachable = False
        status_code = e.response.status_code if hasattr(e, 'response') and e.response is not None else None
        failed_services_details.append({"service": "Metadata Battery", "type": "CONNECTION_ERROR", "status_code": status_code, "message": str(e)})

    return services_reachable, failed_services_details

@program_operation_bp.route('/api/start_program', methods=['POST'])
def start_program():
    global program_runner
    check_result, failed_services_info = check_service_connectivity()
    if not check_result:
        # Construct a more informative error message using the structured details
        # For now, let's just join the 'message' fields for simplicity,
        # but ProgramRunner will use the full structure.
        error_messages = [fs_info.get('message', fs_info.get('service', 'Unknown service error')) for fs_info in failed_services_info]
        error_summary = "Cannot start program: Failed to connect to required services. Failures: " + "; ".join(error_messages) + ". Check logs for details."
        logging.error(error_summary)
        return jsonify({"status": "error", "message": error_summary, "failed_services_details": failed_services_info})

    # --- START EDIT: Check if already running ---
    if program_runner is not None and program_runner.is_running():
        logging.info("Start program request received, but program is already running.")
        return jsonify({"status": "success", "message": "Program is already running"})
    # --- END EDIT ---

    if program_runner is not None:
        # Runner exists but is not running (e.g., stopped previously or failed start)
        logging.warning("Existing non-running program runner found during start request. Stopping and clearing it before creating a new one.")
        program_runner.stop()
        program_runner.invalidate_content_sources_cache()
        program_runner = None
        ProgramRunner._instance = None # Reset the class-level instance tracker
        logging.info("Old ProgramRunner instance cleared and singleton reset before creating new one.")
        time.sleep(1) # Add a small delay to ensure resources are released

    # Add delay if auto-start is enabled (Keep this behavior if desired)
    if get_setting('Debug', 'auto_run_program', default=False):
        time.sleep(1)  # 1 second delay for auto-start

    logging.info("Creating and starting new ProgramRunner instance...") # Added log
    program_runner = ProgramRunner()
    try:
        _setup_scheduler_listeners(program_runner)
    except Exception as e:
        logging.error(f"Failed to set up scheduler listeners during API start: {e}", exc_info=True)
        # Decide if we should abort startup? For now, log and continue.

    # Start the program runner in a separate thread to avoid blocking the Flask server
    threading.Thread(target=program_runner.start).start()
    current_app.config['PROGRAM_RUNNING'] = True

    # Send program start notification
    send_queue_start_notification("Queue processing started via web interface")

    return jsonify({"status": "success", "message": "Program started successfully"}) # Updated success message

def stop_program():
    global program_runner, server_thread
    # --- START EDIT: Log whether runner exists before stopping ---
    if program_runner:
        logging.info("Stop requested. ProgramRunner instance exists.")
    else:
        logging.info("Stop requested. No active ProgramRunner instance found.")
    # --- END EDIT ---
    try:
        if program_runner is not None and program_runner.is_running():
            logging.info("Program is running, proceeding with stop...") # Added log
            # Send stop notification before stopping
            send_queue_stop_notification("Queue processing stopped via web interface")

            program_runner.stop()
            # Invalidate content sources cache before nulling the instance
            program_runner.invalidate_content_sources_cache()
            program_runner = None
            ProgramRunner._instance = None # Reset the class-level instance tracker
            logging.info("ProgramRunner stopped, instance cleared, and singleton reset.") # Updated log

        current_app.config['PROGRAM_RUNNING'] = False
        return {"status": "success", "message": "Program stopped"}
    except Exception as e:
        logging.error(f"Error stopping program: {str(e)}", exc_info=True) # Added exc_info
        # Ensure runner is cleared even on error during stop
        program_runner = None
        ProgramRunner._instance = None # Also reset on error
        current_app.config['PROGRAM_RUNNING'] = False
        return {"status": "error", "message": f"Error stopping program: {str(e)}"}

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

def program_is_initializing():  
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
@user_required
def get_task_timings():
    runner = get_program_runner()
    if not runner or not runner.is_running():
        # If not running, return empty or maybe load saved toggles/intervals?
        # Let's return empty for now, UI can handle loading saved states.
        return jsonify(success=False, error="Program is not running", tasks={
            'queues': {}, 'content_sources': {}, 'system_tasks': {}
        })

    # Load custom intervals (now expects seconds)
    saved_intervals_seconds = {}
    intervals_file_path = _get_task_intervals_file_path()
    if os.path.exists(intervals_file_path):
        try:
            with open(intervals_file_path, 'r') as f:
                saved_intervals_seconds = json.load(f) # Load the saved seconds
        except Exception as e:
            logging.error(f"Error loading saved task intervals (seconds) from {intervals_file_path}: {e}")

    tasks_data = {
        'queues': {},
        'content_sources': {},
        'system_tasks': {}
    }
    job_infos = {}

    try:
        with runner.scheduler_lock:
            if not runner.scheduler or not runner.scheduler.running:
                # Added check inside lock as well
                return jsonify(success=False, error="Scheduler is not running", tasks=tasks_data)

            jobs = runner.scheduler.get_jobs()
            for job in jobs:
                job_infos[job.id] = job

    except Exception as e:
        logging.error(f"Error accessing scheduler jobs: {e}", exc_info=True)
        return jsonify(success=False, error="Failed to access scheduler jobs", tasks=tasks_data)

    now_local = datetime.now(runner.scheduler.timezone) # Use scheduler's timezone

    # Ensure these attributes exist before accessing them
    original_intervals = getattr(runner, 'original_task_intervals', {})
    queue_map = getattr(runner, 'queue_processing_map', {})
    content_sources_map = getattr(runner, 'content_sources', {}) # Use content_sources dict

    all_defined_tasks = set(original_intervals.keys())
    for task_name in all_defined_tasks:
        normalized_name = runner._normalize_task_name(task_name) # Use runner's normalization
        job = job_infos.get(normalized_name)
        
        # Determine the configured interval (custom if set, else default)
        task_default_interval = original_intervals.get(normalized_name, 0)
        task_custom_saved_interval = saved_intervals_seconds.get(normalized_name) # Raw value from JSON (number or None)

        configured_interval = task_default_interval # Start with default
        if task_custom_saved_interval is not None: # If a custom value (a number, not None) is saved
            configured_interval = task_custom_saved_interval
        
        task_info = {
            'enabled': job is not None and job.next_run_time is not None,
            'interval': 0, # Current interval from job or default
            'next_run_in': {'hours': 0, 'minutes': 0, 'seconds': 0, 'total_seconds': 0},
            'display_name': _format_task_display_name(normalized_name, queue_map, content_sources_map),
            'current_interval_seconds': 0, # Actual live interval or configured if disabled
            'default_interval_seconds': task_default_interval,
            'custom_interval_seconds': task_custom_saved_interval, # Raw saved value (number or null)
            'configured_interval_seconds': configured_interval # For the input box
        }

        if job:
            # Get current interval from the job's trigger
            current_job_interval_seconds = 0
            if hasattr(job.trigger, 'interval') and isinstance(job.trigger.interval, timedelta):
                 current_job_interval_seconds = job.trigger.interval.total_seconds()
            task_info['interval'] = current_job_interval_seconds 
            task_info['current_interval_seconds'] = current_job_interval_seconds
            
            if job.next_run_time:
                 # Ensure next_run_time is timezone-aware using scheduler's timezone
                next_run_local = job.next_run_time.astimezone(runner.scheduler.timezone) if job.next_run_time.tzinfo else runner.scheduler.timezone.localize(job.next_run_time)

                time_diff = next_run_local - now_local
                total_seconds = max(0, time_diff.total_seconds())

                task_info['next_run_in'] = {
                    'hours': int(total_seconds // 3600),
                    'minutes': int((total_seconds % 3600) // 60),
                    'seconds': int(total_seconds % 60),
                    'total_seconds': total_seconds
                }
            else:
                 # Job exists but is paused (next_run_time is None)
                 task_info['enabled'] = False # Explicitly set enabled to false if paused

        else:
            # Task is defined but not scheduled (disabled)
            task_info['enabled'] = False
            # For a disabled task, current_interval_seconds reflects what it would be if it started, which is its configured_interval
            task_info['interval'] = configured_interval 
            task_info['current_interval_seconds'] = configured_interval
            # default_interval_seconds, custom_interval_seconds, and configured_interval_seconds are already set above


        # Categorize task
        if normalized_name in queue_map:
            tasks_data['queues'][normalized_name] = task_info
        elif normalized_name.startswith('task_') and normalized_name.endswith('_wanted'):
             # Use the display name logic to check if it was a derived content source
             source_key = normalized_name[5:-7] # Extract potential source key
             is_content_source = False
             if content_sources_map:
                 # Check against actual content source keys
                 # Need to handle potential spaces vs underscores if display name was complex
                 simple_key_match = source_key in content_sources_map
                 # More robust check might involve comparing display names if simple key fails
                 if simple_key_match:
                      is_content_source = True

             if is_content_source:
                 tasks_data['content_sources'][normalized_name] = task_info
             else:
                  # If it looks like a source but isn't in the map, treat as system? Or log warning?
                 logging.warning(f"Task '{normalized_name}' looks like a content source but key '{source_key}' not found in content_sources map. Categorizing as system.")
                 tasks_data['system_tasks'][normalized_name] = task_info
        else:
            tasks_data['system_tasks'][normalized_name] = task_info

    return jsonify(success=True, tasks=tasks_data)

@program_operation_bp.route('/task_timings')
@user_required
def task_timings():
    return render_template('task_timings.html')

@program_operation_bp.route('/trigger_task', methods=['POST'])
@admin_required
def trigger_task():
    task_name = request.form.get('task_name')
    if not task_name:
        return jsonify({'success': False, 'error': 'Task name is required'})
    
    try:
        program_runner = get_program_runner()
        if not program_runner:
            return jsonify({'success': False, 'error': 'Program is not running'})
        
        program_runner.trigger_task(task_name)
        return jsonify({'success': True, 'message': f'Successfully triggered task: {task_name}'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@program_operation_bp.route('/enable_task', methods=['POST'])
@admin_required
def enable_task():
    task_name = request.form.get('task_name')
    if not task_name:
        return jsonify({'success': False, 'error': 'Task name is required'})
    
    try:
        program_runner = get_program_runner()
        if not program_runner:
            return jsonify({'success': False, 'error': 'Program is not running'})
        
        program_runner.enable_task(task_name)
        return jsonify({'success': True, 'message': f'Successfully enabled task: {task_name}'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@program_operation_bp.route('/disable_task', methods=['POST'])
@admin_required
def disable_task():
    task_name = request.form.get('task_name')
    if not task_name:
        return jsonify({'success': False, 'error': 'Task name is required'})
    
    try:
        program_runner = get_program_runner()
        if not program_runner:
            return jsonify({'success': False, 'error': 'Program is not running'})
        
        program_runner.disable_task(task_name)
        return jsonify({'success': True, 'message': f'Successfully disabled task: {task_name}'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@program_operation_bp.route('/save_task_toggles', methods=['POST'])
@admin_required
def save_task_toggles():
    """Save the current state of task toggles to a JSON file, preserving metadata."""
    MIGRATION_VERSION_KEY = "_migration_version"
    try:
        # Get task states from request
        data = request.json
        if not data or 'task_states' not in data:
            return jsonify({'success': False, 'error': 'No task states provided'})
        
        new_task_states = data['task_states']
        if not isinstance(new_task_states, dict):
             return jsonify({'success': False, 'error': 'task_states must be an object'})

        # Get the file path
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        toggles_file_path = os.path.join(db_content_dir, 'task_toggles.json')
        
        current_data = {}
        migration_version = None

        # Read existing file to preserve metadata
        if os.path.exists(toggles_file_path):
            try:
                with open(toggles_file_path, 'r') as f:
                    current_data = json.load(f)
                    if isinstance(current_data, dict) and MIGRATION_VERSION_KEY in current_data:
                        migration_version = current_data[MIGRATION_VERSION_KEY]
            except (json.JSONDecodeError, OSError) as e:
                logging.warning(f"Could not read existing task toggles file to preserve metadata: {e}")
                # Proceed with saving new state, potentially losing metadata

        # Prepare data to save: merge new states with preserved metadata
        data_to_save = new_task_states.copy() # Start with the new states
        if migration_version is not None:
            data_to_save[MIGRATION_VERSION_KEY] = migration_version # Add back the metadata tag

        # Save the combined data to JSON file
        try:
            with open(toggles_file_path, 'w') as f:
                json.dump(data_to_save, f, indent=4)
            logging.info(f"Task toggle states saved to {toggles_file_path}")
            return jsonify({'success': True, 'message': 'Task toggle states saved successfully'})
        except OSError as e:
             logging.error(f"Error writing task toggles file: {str(e)}")
             return jsonify({'success': False, 'error': f"Failed to write file: {str(e)}"})

    except Exception as e:
        logging.error(f"Error saving task toggles: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})

@program_operation_bp.route('/load_task_toggles', methods=['GET'])
@admin_required
def load_task_toggles():
    """Load saved task toggle states from a JSON file."""
    try:
        import os
        import json
        
        # Get the user_db_content directory from environment variable
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        toggles_file_path = os.path.join(db_content_dir, 'task_toggles.json')
        
        # Check if file exists
        if not os.path.exists(toggles_file_path):
            return jsonify({'success': True, 'task_states': {}})
        
        # Load from JSON file
        with open(toggles_file_path, 'r') as f:
            saved_states = json.load(f)
        
        return jsonify({'success': True, 'task_states': saved_states})
    except Exception as e:
        logging.error(f"Error loading task toggles: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})

# --- START EDIT: Helper function for interval file path ---
def _get_task_intervals_file_path():
    """Gets the absolute path for the task_intervals.json file."""
    db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    return os.path.join(db_content_dir, 'task_intervals.json')
# --- END EDIT ---

def _get_task_toggles_file_path():
    """Gets the absolute path for the task_toggles.json file."""
    db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    return os.path.join(db_content_dir, 'task_toggles.json')

# --- START EDIT: Add routes for saving/loading custom intervals ---
@program_operation_bp.route('/save_task_intervals', methods=['POST'])
@admin_required
def save_task_intervals():
    """Saves custom task intervals (in seconds) provided by the user."""
    data = request.get_json()
    if not data or 'task_intervals' not in data:
        return jsonify(success=False, error='Missing task_intervals data'), 400

    custom_intervals_seconds_input = data['task_intervals']
    valid_intervals_seconds = {}
    errors = []
    # --- START EDIT: Define minimum interval in seconds ---
    MIN_INTERVAL_SECONDS = 10 # Example: Minimum 10 seconds
    # --- END EDIT ---

    for task_name, interval_seconds_str in custom_intervals_seconds_input.items():
        if interval_seconds_str is None or interval_seconds_str == '':
             valid_intervals_seconds[task_name] = None # Reset to default
             continue

        try:
            interval_sec = int(interval_seconds_str)
            # --- START EDIT: Validate against minimum seconds ---
            if interval_sec >= MIN_INTERVAL_SECONDS:
                 valid_intervals_seconds[task_name] = interval_sec
            else:
                 errors.append(f"Invalid interval for {task_name}: must be {MIN_INTERVAL_SECONDS} seconds or greater.")
            # --- END EDIT ---
        except (ValueError, TypeError):
            errors.append(f"Invalid interval format for {task_name}: '{interval_seconds_str}' is not a whole number.")

    if errors:
        return jsonify(success=False, error="Validation errors: " + "; ".join(errors)), 400

    intervals_file_path = _get_task_intervals_file_path()
    try:
        # ... (ensure directory exists) ...
        os.makedirs(os.path.dirname(intervals_file_path), exist_ok=True)

        existing_intervals = {}
        if os.path.exists(intervals_file_path):
             try:
                  with open(intervals_file_path, 'r') as f:
                       existing_intervals = json.load(f)
             except json.JSONDecodeError:
                  logging.warning(f"Could not decode existing intervals file {intervals_file_path}. Overwriting.")
                  existing_intervals = {}

        final_intervals_to_save = existing_intervals.copy()
        for task_name, interval_sec in valid_intervals_seconds.items():
             if interval_sec is None:
                  if task_name in final_intervals_to_save:
                       del final_intervals_to_save[task_name]
             else:
                  final_intervals_to_save[task_name] = interval_sec # Save seconds

        with open(intervals_file_path, 'w') as f:
            json.dump(final_intervals_to_save, f, indent=4)

        # Trigger live updates if runner is active
        runner = get_program_runner()
        updated_live = 0
        update_errors = []
        if runner and runner.is_running():
            logging.info("Program running, attempting to apply interval changes (seconds) live...")
            for task_name, interval_sec in valid_intervals_seconds.items(): # Iterate validated seconds
                 if hasattr(runner, 'update_task_interval'):
                     try:
                         # --- START EDIT: Pass seconds to update_task_interval ---
                         if runner.update_task_interval(task_name, interval_sec): # Pass seconds (or None)
                         # --- END EDIT ---
                              updated_live += 1
                     except Exception as live_e:
                         update_errors.append(f"Error applying live update for {task_name}: {live_e}")
                 else:
                      update_errors.append("ProgramRunner does not support live interval updates.")
                      break
            if update_errors:
                 logging.warning("Some live interval updates failed: " + "; ".join(update_errors))

        return jsonify(success=True, message="Task intervals saved (seconds). Changes will apply on next program start." + (f" Attempted to apply {updated_live} changes live." if updated_live > 0 else ""))

    except Exception as e:
        logging.error(f"Error saving task intervals (seconds) to {intervals_file_path}: {str(e)}", exc_info=True)
        return jsonify(success=False, error=f"Failed to save task intervals file: {str(e)}"), 500


@program_operation_bp.route('/load_task_intervals', methods=['GET'])
@admin_required
def load_task_intervals():
    """Loads saved custom task intervals (in seconds)."""
    intervals_file_path = _get_task_intervals_file_path()
    saved_intervals_seconds = {}
    if os.path.exists(intervals_file_path):
        try:
            with open(intervals_file_path, 'r') as f:
                saved_intervals_seconds = json.load(f) # Load seconds
        except Exception as e:
            logging.error(f"Error loading saved task intervals (seconds) from {intervals_file_path}: {e}")
            # Return empty on error but indicate success to not break UI, error is logged
            return jsonify(success=True, task_intervals={})

    return jsonify(success=True, task_intervals=saved_intervals_seconds) # Return seconds

# --- START EDIT: Modify reset_all_task_settings endpoint ---
@program_operation_bp.route('/api/reset_all_task_settings', methods=['POST'])
@admin_required
def reset_all_task_settings():
    toggles_file_path = _get_task_toggles_file_path()
    intervals_file_path = _get_task_intervals_file_path()
    files_deleted_count = 0
    deletion_errors = []
    files_not_found_count = 0

    try:
        # Delete settings files
        for file_path, file_desc in [(toggles_file_path, "task toggles"), (intervals_file_path, "task intervals")]:
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    files_deleted_count += 1
                    logging.info(f"Deleted {file_desc} file: {file_path}")
                except OSError as e:
                    logging.error(f"Error deleting {file_desc} file {file_path}: {e}")
                    deletion_errors.append(f"Error deleting {file_desc} file: {str(e)}")
            else:
                files_not_found_count +=1
                logging.info(f"{file_desc} file not found, no action needed: {file_path}")
        
        if deletion_errors:
            error_message = f"Errors occurred while deleting settings files: {'; '.join(deletion_errors)}. {files_deleted_count} files deleted. Restart the program for changes to take effect."
            logging.error(error_message)
            return jsonify({
                "status": "error", 
                "message": error_message,
                "files_deleted": files_deleted_count,
                "deletion_errors": deletion_errors
            }), 500

        success_message = f"Task settings reset. {files_deleted_count} configuration files deleted."
        if files_not_found_count > 0:
            success_message += f" {files_not_found_count} files were already absent."
        success_message += " Please restart the program for changes to take full effect."
        
        logging.info(success_message)
        return jsonify({"status": "success", "message": success_message, "files_deleted": files_deleted_count})

    except Exception as e:
        logging.error(f"Critical error in reset_all_task_settings: {str(e)}", exc_info=True)
        return jsonify({'status': 'error', 'message': f"An unexpected critical error occurred during reset: {str(e)}"}), 500
# --- END EDIT ---

# --- START EDIT: Define the missing helper function ---
def _format_task_display_name(task_name, queue_map, content_sources_map):
    """Formats the internal task name into a user-friendly display name."""
    
    # Check if it's a known queue task (using the map keys)
    if task_name in queue_map:
        # For queues, the key itself is usually descriptive enough
        # Maybe capitalize? e.g., "Wanted", "Scraping"
        return task_name.capitalize()

    # Check if it's a content source task
    if task_name.startswith('task_') and task_name.endswith('_wanted'):
        source_key = task_name[5:-7] # Extract potential key 'My_Overseerr' etc.
        
        # Try to find a matching entry in the content_sources_map
        # --- START EDIT: Prioritize configured display_name ---
        if content_sources_map and source_key in content_sources_map:
            source_config = content_sources_map[source_key]
            # Check if the source config has a non-empty 'display_name' field
            config_display_name = getattr(source_config, 'display_name', '') or source_config.get('display_name', '') if isinstance(source_config, dict) else ''

            if config_display_name and config_display_name.strip():
                return config_display_name.strip()
            else:
                # Fallback to formatting the key if display_name is missing/empty
                display_name = source_key.replace('_', ' ').strip()
                # Basic capitalization (capitalize first letter of each word)
                return ' '.join(word.capitalize() for word in display_name.split())
        # --- END EDIT ---
        else:
            # Fallback if key not found in map (shouldn't happen often if maps are synced)
             logging.warning(f"Could not find source key '{source_key}' in content_sources_map for display name formatting.")
             # Fallback to generic formatting
             display_name = source_key.replace('_', ' ').strip()
             return ' '.join(word.capitalize() for word in display_name.split()) + " (Source)"


    # Handle other system tasks (usually start with 'task_')
    if task_name.startswith('task_'):
        display_name = task_name[5:].replace('_', ' ').strip() # Remove prefix, replace underscores
         # Capitalize first letter of each word
        return ' '.join(word.capitalize() for word in display_name.split())

    # Default fallback if no rule matches (should be rare)
    return task_name.replace('_', ' ').capitalize()
# --- END EDIT ---
