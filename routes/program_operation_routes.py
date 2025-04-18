from flask import jsonify, request, current_app, Blueprint, logging, render_template
from routes import admin_required, user_required
from .database_routes import perform_database_migration 
from routes.extensions import initialize_app 
from queues.config_manager import load_config 
from utilities.settings import get_setting
import threading
from queues.run_program import ProgramRunner
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
from datetime import datetime
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
    failed_services = []

    # Check Symlink paths if using symlink management
    if get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
        original_path = get_setting('File Management', 'original_files_path')
        symlinked_path = get_setting('File Management', 'symlinked_files_path')
        
        # Check original files path
        if not os.path.exists(original_path):
            logging.error(f"Cannot access original files path: {original_path}")
            services_reachable = False
            failed_services.append(f"Original files path ({original_path})")
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
                failed_services.append(f"Symlinked files path ({symlinked_path})")
                
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
                    failed_services.append("Plex (for symlink updates)")
                else:
                    services_reachable = True  # Set to True when Plex is reachable
                    logging.debug("Plex connectivity check passed")
            except (RequestException, ET.ParseError) as e:
                error_msg = f"Cannot connect to Plex server for symlink updates. Error: {str(e)}"
                logging.error(error_msg)
                services_reachable = False
                failed_services.append("Plex (for symlink updates)")

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
                    failed_services.append("Plex (invalid token)")
                    return services_reachable, failed_services
                else:
                    logging.info(f"Successfully validated Plex connection (Server: {root.get('friendlyName', 'Unknown')})")
            except ET.ParseError as e:
                error_msg = f"Invalid Plex response format: {str(e)}"
                logging.error(error_msg)
                services_reachable = False
                failed_services.append("Plex (invalid response format)")
                return services_reachable, failed_services

            # Then check library existence
            libraries_response = api.get(f"{plex_url}/library/sections?X-Plex-Token={plex_token}", timeout=5)
            libraries_response.raise_for_status()
            
            # Get configured library names
            movie_libraries = [lib.strip() for lib in get_setting('Plex', 'movie_libraries', '').split(',') if lib.strip()]
            show_libraries = [lib.strip() for lib in get_setting('Plex', 'shows_libraries', '').split(',') if lib.strip()]
            
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
                
                if not available_libraries:
                    logging.error("No libraries found in Plex response")
                    services_reachable = False
                    failed_services.append("Plex (no libraries found)")
                    return services_reachable, failed_services

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
                    failed_services.append(f"Plex (missing libraries: {', '.join(missing_libraries)})")
                    return services_reachable, failed_services

            except ET.ParseError as e:
                error_msg = f"Failed to parse Plex libraries response (XML): {str(e)}"
                logging.error(error_msg)
                services_reachable = False
                failed_services.append("Plex (invalid libraries response)")
                return services_reachable, failed_services

        except RequestException as e:
            error_msg = f"Cannot start program: Failed to connect to Plex server. Error: {str(e)}"
            logging.error(error_msg)
            services_reachable = False
            failed_services.append("Plex (connection error)")

    # Check Debrid Provider connectivity
    if debrid_provider.lower() == 'realdebrid':
        try:
            response = api.get("https://api.real-debrid.com/rest/1.0/user", headers={"Authorization": f"Bearer {debrid_api_key}"}, timeout=5)
            response.raise_for_status()
        except RequestException as e:
            logging.error(f"Failed to connect to Real-Debrid API: {str(e)}")
            services_reachable = False
            failed_services.append("Real-Debrid API")
    else:
        logging.error(f"Unknown debrid provider: {debrid_provider}")
        services_reachable = False
        failed_services.append(f"Unknown debrid provider ({debrid_provider})")

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
            failed_services.append("Trakt (not authorized)")
    except RequestException as e:
        if hasattr(e, 'response') and e.response is not None:
            logging.error(f"Failed to connect to Metadata Battery: {e.response.status_code} {e.response.reason}")
            logging.error(f"Response content: {e.response.text}")
        else:
            logging.error(f"Failed to connect to Metadata Battery: {str(e)}")
        services_reachable = False
        failed_services.append("Metadata Battery")

    return services_reachable, failed_services

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
    check_result, failed_services = check_service_connectivity()
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
        return jsonify({"status": "error", "message": error_message, "failed_services": failed_services})

    program_runner = ProgramRunner()
    # Start the program runner in a separate thread to avoid blocking the Flask server
    threading.Thread(target=program_runner.start).start()
    current_app.config['PROGRAM_RUNNING'] = True
    
    # Send program start notification
    send_queue_start_notification("Queue processing started via web interface")
    
    return jsonify({"status": "success", "message": "Program started"})

def stop_program():
    global program_runner, server_thread
    try:
        if program_runner is not None and program_runner.is_running():
            # Send stop notification before stopping
            send_queue_stop_notification("Queue processing stopped via web interface")
            
            program_runner.stop()
            # Invalidate content sources cache before nulling the instance
            program_runner.invalidate_content_sources_cache()
            program_runner = None
            
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

    # If program runner isn't initialized, return empty immediately
    if not program_runner or not hasattr(program_runner, 'task_intervals'):
        logging.debug("Program runner not fully initialized, returning empty task timings.")
        return jsonify({
            "success": True,
            "tasks": {"queues": {}, "content_sources": {}, "system_tasks": {}}
        })

    # Load saved toggle states to determine the intended state if a task isn't scheduled
    saved_states = {}
    try:
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        toggles_file_path = os.path.join(db_content_dir, 'task_toggles.json')
        if os.path.exists(toggles_file_path):
            with open(toggles_file_path, 'r') as f:
                saved_states = json.load(f)
    except Exception as e:
        logging.error(f"Error loading task_toggles.json: {e}")

    # Get all defined tasks and their default intervals
    defined_tasks_with_intervals = program_runner.task_intervals or {}

    # Get currently scheduled jobs if scheduler is running
    scheduled_jobs_dict = {}
    tz = pytz.utc # Default timezone
    current_time_dt = datetime.now(tz)
    scheduler_running = False
    if hasattr(program_runner, 'scheduler') and program_runner.scheduler.running:
        scheduler_running = True
        # Use scheduler's timezone if available
        tz = program_runner.scheduler.timezone if hasattr(program_runner.scheduler, 'timezone') else pytz.utc
        current_time_dt = datetime.now(tz)
        with program_runner.scheduler_lock: # Use lock for safety
            jobs = program_runner.scheduler.get_jobs()
            scheduled_jobs_dict = {job.id: job for job in jobs}
            logging.debug(f"Found {len(scheduled_jobs_dict)} scheduled jobs.")
    else:
        logging.debug("Scheduler not running or not found.")


    # Ensure content sources are loaded (for display names)
    # Use force_refresh=False to avoid redundant work if already loaded
    content_sources = program_runner.get_content_sources(force_refresh=False) or {}

    all_tasks_data = {}

    # Iterate through all DEFINED tasks
    for task_name, defined_interval in defined_tasks_with_intervals.items():
        job = scheduled_jobs_dict.get(task_name)
        is_scheduled = job is not None
        live_enabled = is_scheduled and job.next_run_time is not None

        interval = defined_interval # Use the defined interval as default
        next_run_timestamp = None
        time_until_next_run = 0

        if is_scheduled:
            # Use interval from the job trigger if available and it's an IntervalTrigger
            if isinstance(job.trigger, IntervalTrigger):
                 interval = job.trigger.interval.total_seconds()

            if job.next_run_time:
                # Ensure next_run_time is timezone-aware using scheduler's timezone
                next_run_aware = job.next_run_time.astimezone(tz)
                next_run_timestamp = next_run_aware.timestamp()
                # Calculate time until next run in seconds only if live_enabled
                if live_enabled:
                     time_until_next_run = max(0, next_run_timestamp - current_time_dt.timestamp())

        # Convert to hours, minutes, seconds
        hours, remainder = divmod(int(time_until_next_run), 3600)
        minutes, seconds = divmod(remainder, 60)

        # Create a human-readable display name (reuse existing logic block)
        display_name = task_name # Default
        normalized_task_name_for_display = program_runner._normalize_task_name(task_name) # Normalize for consistent lookup

        if normalized_task_name_for_display in ['Wanted', 'Scraping', 'Adding', 'Checking', 'Sleeping',
                       'Unreleased', 'Blacklisted', 'Pending Uncached', 'Upgrading']:
            display_name = normalized_task_name_for_display
        elif normalized_task_name_for_display.endswith('_wanted'):
            source_name = normalized_task_name_for_display.replace('task_', '').replace('_wanted', '')
            source_config = content_sources.get(source_name) # Use normalized name lookup
            if isinstance(source_config, dict) and source_config.get('display_name'):
                display_name = source_config['display_name']
            else:
                # Fallback formatting using the derived source_name
                display_name = ' '.join(word.capitalize() for word in source_name.split('_'))
        elif normalized_task_name_for_display.startswith('task_'):
             display_name = ' '.join(word.capitalize() for word in normalized_task_name_for_display.replace('task_', '').split('_'))
        # Keep original task_name as fallback if no rule matched
        else:
             display_name = task_name


        all_tasks_data[task_name] = {
            "display_name": display_name,
            "next_run_in": {
                "hours": hours,
                "minutes": minutes,
                "seconds": seconds,
                "total_seconds": time_until_next_run
            },
            "interval": interval, # Use interval from job or defined default
            "last_run": None, # Last run time is not easily available
             # 'enabled' reflects LIVE status if scheduled, FALSE otherwise.
             # This matches frontend expectation for styling/labels.
            "enabled": live_enabled
        }

    # Group tasks by type (using the combined data)
    grouped_timings = {
        "queues": {},
        "content_sources": {},
        "system_tasks": {}
    }

    # Use the same grouping logic as before, applying it to all_tasks_data
    for task, timing in all_tasks_data.items():
        normalized_task_name = program_runner._normalize_task_name(task) # Normalize for consistent checks
        if normalized_task_name in ['Wanted', 'Scraping', 'Adding', 'Checking', 'Sleeping',
                   'Unreleased', 'Blacklisted', 'Pending Uncached', 'Upgrading']:
            # Use the original task name (which might be non-normalized if that's the key)
            # Or better, use the normalized name as the key for consistency
            grouped_timings["queues"][normalized_task_name] = timing
        elif normalized_task_name.endswith('_wanted'):
             # Use normalized name as key
            grouped_timings["content_sources"][normalized_task_name] = timing
        else:
            # Only include tasks that start with 'task_' in system tasks (using normalized name)
            if normalized_task_name.startswith('task_'):
                 # Use normalized name as key
                 grouped_timings["system_tasks"][normalized_task_name] = timing
            # else: # Log tasks that didn't fit into any category?
            #    logging.debug(f"Task '{task}' (normalized: '{normalized_task_name}') did not fit into known categories.")

    return jsonify({
        "success": True,
        "tasks": grouped_timings,
        "current_time": current_time_dt.timestamp() # Send current timestamp for reference
    })

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
