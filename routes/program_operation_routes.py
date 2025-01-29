from flask import jsonify, request, current_app, Blueprint, logging, render_template
from routes import admin_required, user_required
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
import signal
import psutil
import sys
import subprocess
import xml.etree.ElementTree as ET

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
    from extensions import app
    
    # Get port from environment variable or use default
    port = int(os.environ.get('CLI_DEBRID_PORT', 5000))
    try:
        app.run(debug=True, use_reloader=False, host='0.0.0.0', port=port)
    except Exception as e:
        logging.error(f"Error running server: {str(e)}")
        cleanup_port(port)

def start_server():
    from extensions import app
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

    # Check Plex connectivity and libraries
    if get_setting('File Management', 'file_collection_management') == 'Plex':
        try:
            # First check basic connectivity
            response = api.get(f"{plex_url}?X-Plex-Token={plex_token}", timeout=5)
            response.raise_for_status()

            # Then check library existence
            libraries_response = api.get(f"{plex_url}/library/sections?X-Plex-Token={plex_token}", timeout=5)
            libraries_response.raise_for_status()
            
            # Get configured library names
            movie_libraries = [lib.strip() for lib in get_setting('Plex', 'movie_libraries', '').split(',') if lib.strip()]
            show_libraries = [lib.strip() for lib in get_setting('Plex', 'shows_libraries', '').split(',') if lib.strip()]
            
            #logging.info("Configured Plex libraries:")
            #logging.info(f"Movie libraries: {movie_libraries}")
            #logging.info(f"TV Show libraries: {show_libraries}")
            
            try:
                # Get actual library names from Plex (XML format)
                available_libraries = []
                library_id_to_title = {}  # Map to store ID -> Title mapping
                root = ET.fromstring(libraries_response.text)
                for directory in root.findall('.//Directory'):
                    library_title = directory.get('title')
                    library_key = directory.get('key')
                    if library_title and library_key:
                        available_libraries.append(library_title)
                        library_id_to_title[library_key] = library_title
                        logging.info(f"Found Plex library: ID={library_key}, Title='{library_title}', Type={directory.get('type')}")
                
                #logging.info("Available Plex libraries:")
                #logging.info(f"Found libraries: {available_libraries}")
                #logging.info(f"Library ID mapping: {library_id_to_title}")
                        
                if not available_libraries:
                    logging.error("No libraries found in Plex response")
                    services_reachable = False
                    return services_reachable

                # Verify all configured libraries exist (check both IDs and names)
                missing_libraries = []
                for lib in movie_libraries + show_libraries:
                    # Check if the library exists either as a title or an ID
                    if lib not in available_libraries and lib not in library_id_to_title:
                        # If it's a number, try to show the expected title
                        if lib.isdigit() and lib in library_id_to_title:
                            logging.info(f"Library ID {lib} refers to library '{library_id_to_title[lib]}'")
                        else:
                            missing_libraries.append(lib)
                            logging.warning(f"Library '{lib}' not found in available libraries")

                if missing_libraries:
                    error_msg = "Cannot start program: The following Plex libraries were not found:<ul>"
                    for lib in missing_libraries:
                        error_msg += f"<li>{lib}</li>"
                    error_msg += "</ul>Available libraries are:<ul>"
                    for title in available_libraries:
                        error_msg += f"<li>{title}</li>"
                    error_msg += "</ul>Please verify your Plex library names in settings."
                    logging.error(error_msg)
                    services_reachable = False
                    return services_reachable

            except ET.ParseError as e:
                error_msg = f"Failed to parse Plex libraries response (XML): {str(e)}"
                logging.error(error_msg)
                services_reachable = False
                return services_reachable

        except RequestException as e:
            error_msg = f"Cannot start program: Failed to connect to Plex server. Error: {str(e)}"
            logging.error(error_msg)
            services_reachable = False

    # Check Debrid Provider connectivity
    if debrid_provider.lower() == 'realdebrid':
        try:
            response = api.get("https://api.real-debrid.com/rest/1.0/user", headers={"Authorization": f"Bearer {debrid_api_key}"}, timeout=5)
            response.raise_for_status()
        except RequestException as e:
            logging.error(f"Failed to connect to Real-Debrid API: {str(e)}")
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
    if program_runner is not None:
        # Always clean up existing instance
        program_runner.stop()
        program_runner.invalidate_content_sources_cache()
        program_runner = None

    # Add delay if auto-start is enabled
    if get_setting('Debug', 'auto_run_program', default=False):
        time.sleep(1)  # 1 second delay for auto-start

    # Check service connectivity before starting the program
    check_result = check_service_connectivity()
    if not check_result:
        # Get the last error message from the logs
        error_message = "Failed to connect to required services. Check the logs for details."
        for handler in logging.getLogger().handlers:
            if isinstance(handler, logging.StreamHandler):
                try:
                    # Get the last error message if available
                    error_message = handler.stream.getvalue().strip().split('\n')[-1]
                except:
                    pass
        return jsonify({"status": "error", "message": error_message})

    program_runner = ProgramRunner()
    # Start the program runner in a separate thread to avoid blocking the Flask server
    threading.Thread(target=program_runner.start).start()
    current_app.config['PROGRAM_RUNNING'] = True
    return jsonify({"status": "success", "message": "Program started"})

def stop_program():
    global program_runner, server_thread
    try:
        if program_runner is not None and program_runner.is_running():
            program_runner.stop()
            # Invalidate content sources cache before nulling the instance
            program_runner.invalidate_content_sources_cache()
            program_runner = None
            
        # Cleanup port
        # port = int(os.environ.get('CLI_DEBRID_PORT', 5000))
        # cleanup_port(port)
        
        current_app.config['PROGRAM_RUNNING'] = False
        return {"status": "success", "message": "Program stopped"}
    except Exception as e:
        logging.error(f"Error stopping program: {str(e)}")
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
    global program_runner
    
    if not program_runner or not program_runner.is_running():
        return jsonify({
            "success": True,
            "current_task": None,
            "tasks": []
        })

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
        "success": True,
        "tasks": grouped_timings,
        "current_time": current_time
    })

@program_operation_bp.route('/task_timings')
@user_required
def task_timings():
    return render_template('task_timings.html')
