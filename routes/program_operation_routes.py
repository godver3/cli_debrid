from flask import jsonify, request, current_app, Blueprint, logging, render_template, flash, redirect, url_for
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
from content_checkers.trakt import is_refresh_token_expired

program_operation_bp = Blueprint('program_operation', __name__)

_program_op_lock = threading.Lock() # Global lock for start/stop operations
program_runner = None
server_thread = None

def get_program_runner():
    # The canonical instance is stored in the app context.
    return getattr(current_app, 'program_runner', None)

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
    from werkzeug.serving import make_server, WSGIRequestHandler

    # Get port from environment variable or use default
    port = int(os.environ.get('CLI_DEBRID_PORT', 5000))

    class WebSocketAwareHandler(WSGIRequestHandler):
        """Threaded Werkzeug handler that supports SignalR WebSocket upgrades.

        SignalR WebSocket sessions are handled here directly (not via Flask) so
        app before_request / session middleware cannot block the 101 handshake.
        """

        protocol_version = "HTTP/1.1"

        def make_environ(self):
            environ = super().make_environ()
            environ['werkzeug.socket'] = self.connection
            return environ

        def run_wsgi(self):
            upgrade = (self.headers.get('Upgrade') or '').lower().strip()
            path = (self.path or '').split('?', 1)[0]
            if upgrade == 'websocket' and path in ('/signalr/messages', '/signalr'):
                self.environ = environ = self.make_environ()
                try:
                    from routes.bazarr_spoofing_routes import run_signalr_websocket_session
                    run_signalr_websocket_session(environ)
                except (BrokenPipeError, ConnectionError, ConnectionResetError):
                    pass
                except Exception as e:
                    logging.error(f"[SignalR] WebSocket handler error: {e}", exc_info=True)
                return
            return super().run_wsgi()

    try:
        # Use make_server (not app.run) so we control the request handler and
        # avoid the Flask debugger wrapper interfering with Upgrade requests.
        logging.info(f"Starting Werkzeug threaded server with WebSocket support on 0.0.0.0:{port}")
        server = make_server(
            '0.0.0.0',
            port,
            app,
            threaded=True,
            request_handler=WebSocketAwareHandler,
        )
        server.serve_forever()
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

        # Check Jellyfin/Emby connectivity for symlink updates if enabled
        jellyfin_url = get_setting('Debug', 'emby_jellyfin_url')
        jellyfin_token = get_setting('Debug', 'emby_jellyfin_token')
        
        if jellyfin_url and jellyfin_token:
            try:
                # Ensure URL ends with a slash
                if not jellyfin_url.endswith('/'):
                    jellyfin_url += '/'
                
                system_info_url = f"{jellyfin_url}System/Info"
                
                headers = {
                    'X-Emby-Token': jellyfin_token,
                    'Accept': 'application/json'
                }
                
                response = api.get(system_info_url, headers=headers, timeout=5)
                response.raise_for_status()
                
                # Verify we got a valid Jellyfin/Emby response
                server_info = response.json()
                if not server_info.get('ServerName') or not server_info.get('Version'):
                    error_msg = "Invalid Jellyfin/Emby response for symlink updates - token may be incorrect"
                    logging.error(error_msg)
                    services_reachable = False
                    failed_services_details.append({"service": "Jellyfin/Emby (for symlink updates)", "type": "INVALID_TOKEN", "message": error_msg})
                else:
                    services_reachable = True  # Set to True when Jellyfin/Emby is reachable
                    logging.debug(f"Jellyfin/Emby connectivity check passed (Server: {server_info.get('ServerName', 'Unknown')})")
            except (RequestException, ValueError) as e:
                error_msg = f"Cannot connect to Jellyfin/Emby server for symlink updates. Error: {str(e)}"
                logging.error(error_msg)
                services_reachable = False
                failed_services_details.append({"service": "Jellyfin/Emby (for symlink updates)", "type": "CONNECTION_ERROR", "status_code": None, "message": error_msg})

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

    # Check provider connectivity — requires at least debrid OR usenet (cli_mount) to be configured
    debrid_configured = bool(debrid_provider and debrid_api_key)
    usenet_enabled = get_setting('Usenet Provider', 'enabled', False)
    usenet_url = get_setting('Usenet Provider', 'url', '')

    if not debrid_configured and not usenet_enabled:
        services_reachable = False
        failed_services_details.append({
            "service": "Provider",
            "type": "NO_PROVIDER_CONFIGURED",
            "message": "No provider configured — set up a Debrid provider or a Usenet provider (cli_mount) to continue"
        })
    elif debrid_configured:
        # Check Debrid Provider connectivity
        try:
            from debrid import get_debrid_provider
            provider = get_debrid_provider()
            if provider and hasattr(provider, 'check_connectivity'):
                ok, error_detail = provider.check_connectivity()
                if not ok:
                    services_reachable = False
                    failed_services_details.append(error_detail or {"service": "Debrid Provider API", "type": "CONNECTION_ERROR", "status_code": None, "message": "Connectivity check failed"})
            else:
                logging.info("Provider does not implement connectivity check; skipping provider connectivity validation.")

            # Verify subscription days remaining > 0
            try:
                subscription_info = None
                if provider and hasattr(provider, 'get_subscription_status'):
                    subscription_info = provider.get_subscription_status()
                if subscription_info is not None:
                    days_remaining = subscription_info.get('days_remaining')
                    premium = subscription_info.get('premium')
                    expiration = subscription_info.get('expiration')
                    logging.info(f"Debrid subscription status: days_remaining={days_remaining}, premium={premium}, expiration={expiration}")
                    if days_remaining is None or not isinstance(days_remaining, (int, float)) or days_remaining <= 0:
                        services_reachable = False
                        failed_services_details.append({
                            "service": "Debrid Subscription",
                            "type": "SUBSCRIPTION_EXPIRED",
                            "message": f"Debrid subscription expired or invalid. days_remaining={days_remaining}, premium={premium}, expiration={expiration}"
                        })
                else:
                    logging.warning("Debrid subscription status unavailable from provider; skipping days remaining check")
            except Exception as sub_err:
                logging.error(f"Error checking Debrid subscription status: {sub_err}")
                services_reachable = False
                failed_services_details.append({
                    "service": "Debrid Subscription",
                    "type": "SUBSCRIPTION_CHECK_ERROR",
                    "message": str(sub_err)
                })
        except Exception as e:
            logging.error(f"Failed to perform provider connectivity check: {e}")
            services_reachable = False
            failed_services_details.append({"service": "Debrid Provider API", "type": "CONNECTION_ERROR", "status_code": None, "message": str(e)})

    if usenet_enabled and usenet_url:
        # Provider-agnostic connectivity check via usenet-client factory.
        # Supports cli_mount (default) and NzbDAV via 'Usenet Provider.provider'.
        try:
            from usenet import get_usenet_client
            _client = get_usenet_client()
            _provider_label = getattr(_client, 'PROVIDER_NAME', 'Usenet Provider')
            _ok, _err = _client.check_connectivity()
            if not _ok:
                services_reachable = False
                failed_services_details.append({
                    "service": f"Usenet Provider ({_provider_label})",
                    "type": "CONNECTION_ERROR",
                    "status_code": None,
                    "message": f"Cannot reach {_provider_label} at {usenet_url}: {_err}"
                })
            else:
                logging.info(f"Usenet provider ({_provider_label}) reachable at {usenet_url}")
        except Exception as _ue:
            services_reachable = False
            failed_services_details.append({
                "service": "Usenet Provider",
                "type": "CONNECTION_ERROR",
                "status_code": None,
                "message": f"Connectivity check error: {_ue}"
            })

    # Check Trakt authorization — only if the user has actually configured Trakt.
    # Trakt is an optional integration; an unconfigured or unauthorized Trakt
    # account must never block program start or pause the running queues.
    trakt_client_id = get_setting('Trakt', 'client_id')
    trakt_client_secret = get_setting('Trakt', 'client_secret')

    if trakt_client_id and trakt_client_secret:
        try:
            from cli_battery.app.direct_api import DirectAPI
            result = DirectAPI.check_trakt_auth()
            trakt_status = result.get('status')

            if trakt_status != 'authorized':
                logging.warning("Trakt is configured, but not authorized.")

                # Attempt automatic re-authentication
                auto_reauth_success = attempt_trakt_auto_reauth()
                if auto_reauth_success:
                    logging.info("Automatic Trakt re-authentication successful. Re-checking authorization...")
                    result = DirectAPI.check_trakt_auth()
                    trakt_status = result.get('status')

                    if trakt_status == 'authorized':
                        logging.info("Trakt authorization restored after automatic re-authentication.")
                    else:
                        logging.warning("Trakt still not authorized after automatic re-authentication attempt. Continuing without Trakt.")
                else:
                    logging.warning("Automatic Trakt re-authentication failed. Continuing without Trakt.")

        except Exception as e:
            logging.error(f"Error checking Trakt auth via battery (non-fatal, Trakt is optional): {e}")
    else:
        logging.debug("Trakt is not configured; skipping Trakt authorization check.")

    return services_reachable, failed_services_details

def attempt_trakt_auto_reauth():
    """
    Attempt to automatically re-authenticate Trakt and push the new credentials to Metadata Battery.
    Returns True if successful, False otherwise.
    """
    try:
        logging.info("Attempting automatic Trakt re-authentication...")
        
        # First, try to refresh the token using the existing ensure_trakt_auth function
        from content_checkers.trakt import ensure_trakt_auth, is_refresh_token_expired
        access_token = ensure_trakt_auth()
        
        if not access_token:
            # Check if the failure was due to expired refresh token
            if is_refresh_token_expired():
                logging.warning("Refresh token has expired. Manual re-authentication is required.")
                return False
            
            logging.warning("Could not obtain valid Trakt access token during auto-reauth")
            return False
        
        # If we got a valid token, push it to the Metadata Battery
        from routes.trakt_routes import push_trakt_auth_to_battery_core
        success, message = push_trakt_auth_to_battery_core()
        
        if success:
            logging.info("Successfully pushed refreshed Trakt credentials to Metadata Battery")
            return True
        else:
            logging.error(f"Failed to push Trakt credentials to Metadata Battery: {message}")
            return False
            
    except Exception as e:
        logging.error(f"Error during automatic Trakt re-authentication: {str(e)}", exc_info=True)
        return False

# --- START EDIT: New internal function for program start logic ---
def _execute_start_program(skip_connectivity_check: bool = False, is_restart: bool = False):
    """
    Core logic to start the program.
    This is intended to be called internally by main.py or by the Flask route.
    It relies on current_app.program_runner being the target instance.
    Returns:
        dict: {'status': 'success'/'error', 'message': str, 'failed_services_details': list (optional)}
    """
    if not _program_op_lock.acquire(blocking=False):
        logging.warning("[_execute_start_program] Could not acquire lock, another operation in progress.")
        return {"status": "error", "message": "System busy: Another start/stop operation is in progress. Please try again shortly."}

    try:
        logging.info("--- Entered _execute_start_program logic ---")

        # The ProgramRunner instance is expected to be on current_app, set by main.py's __main__
        runner_instance = getattr(current_app, 'program_runner', None)

        if not runner_instance:
            logging.error("[_execute_start_program] CRITICAL: current_app.program_runner is NOT SET. Cannot start.")
            return {"status": "error", "message": "Program runner not initialized in application context."}

        logging.info(f"[_execute_start_program] Found runner_instance (ID: {id(runner_instance)}) on current_app. Is_running: {runner_instance.is_running()}, Is_initializing: {runner_instance.is_initializing()}, Is_stopping: {hasattr(runner_instance, 'is_stopping') and runner_instance.is_stopping()}, Listeners_Setup_Flag: {getattr(runner_instance, 'initial_listeners_setup_complete', 'N/A')}, Scheduler_Exists: {hasattr(runner_instance, 'scheduler')}, Scheduler_Is_None: {getattr(runner_instance, 'scheduler', 'AttrMissing') is None}")

        if hasattr(runner_instance, 'is_stopping') and runner_instance.is_stopping():
            logging.warning(f"[_execute_start_program] Attempt to start while program is stopping (runner ID: {id(runner_instance)}).")
            return {"status": "error", "message": "Program is currently stopping. Cannot start at this moment."}

        # Service connectivity check (optional)
        if not skip_connectivity_check:
            check_result, failed_services_info = check_service_connectivity()
            if not check_result:
                error_messages = [fs_info.get('message', fs_info.get('service', 'Unknown service error')) for fs_info in failed_services_info]
                error_summary = "Cannot start program: Failed to connect to required services. Failures: " + "; ".join(error_messages)
                logging.error(f"[_execute_start_program] Service connectivity check FAILED: {error_summary}")
                return {"status": "error", "message": error_summary, "failed_services_details": failed_services_info}
            logging.info("[_execute_start_program] Service connectivity check PASSED.")
        else:
            logging.info("[_execute_start_program] Pre-start connectivity check skipped as requested.")

        if runner_instance.is_running():
            logging.info(f"[_execute_start_program] Program (runner_instance ID: {id(runner_instance)}) is already running.")
            return {"status": "success", "message": "Program is already running"}

        try:
            needs_listener_setup = False
            if not getattr(runner_instance, 'initial_listeners_setup_complete', False):
                needs_listener_setup = True
                logging.info(f"[_execute_start_program] Reason for listener setup: initial_listeners_setup_complete is False for runner (ID: {id(runner_instance)}).")
            elif not hasattr(runner_instance, 'scheduler') or runner_instance.scheduler is None: # Explicitly set to None by stop_program
                needs_listener_setup = True
                logging.info(f"[_execute_start_program] Reason for listener setup: runner.scheduler is missing or None for runner (ID: {id(runner_instance)}).")
            # If scheduler exists but isn't running (shouldn't happen if set to None on stop, but good check)
            elif hasattr(runner_instance, 'scheduler') and runner_instance.scheduler is not None and not runner_instance.scheduler.running:
                 needs_listener_setup = True
                 logging.info(f"[_execute_start_program] Reason for listener setup: runner.scheduler exists but is not running for runner (ID: {id(runner_instance)}). Attempting to clear it.")
                 # Attempt to shut down and nullify this zombie scheduler
                 try:
                     # Only attempt shutdown if the scheduler somehow thinks it's running;
                     # calling shutdown() on a never-started scheduler needlessly raises an exception.
                     if runner_instance.scheduler.running:
                         runner_instance.scheduler.shutdown(wait=False)
                 except Exception as e_shutdown_zombie:
                     logging.warning(f"Error shutting down existing non-running scheduler: {e_shutdown_zombie}")
                 runner_instance.scheduler = None


            if needs_listener_setup:
                logging.info(f"[_execute_start_program] Attempting to set up/re-setup scheduler listeners for runner_instance (ID: {id(runner_instance)}).")
                _setup_scheduler_listeners(runner_instance) # This function MUST handle runner.scheduler being None by creating a new one
                runner_instance.initial_listeners_setup_complete = True
                logging.info(f"[_execute_start_program] Scheduler listeners setup successful for runner_instance (ID: {id(runner_instance)}).")
            else:
                logging.info(f"[_execute_start_program] Scheduler listeners for runner_instance (ID: {id(runner_instance)}) appear to be correctly set up and scheduler is running.")
        
        except Exception as e:
            logging.error(f"[_execute_start_program] CRITICAL: Failed to set up scheduler listeners for runner_instance (ID: {id(runner_instance)}): {e}", exc_info=True)
            return {
                "status": "error",
                "message": "Failed to set up critical scheduler listeners. Program cannot start.",
                "failed_services_details": [{"service": "SchedulerSetup", "type": "INITIALIZATION_ERROR", "message": f"Error setting up listeners: {str(e)}"}]
            }

        if get_setting('Debug', 'auto_run_program', default=False) and not current_app.config.get('PROGRAM_AUTO_STARTED_THIS_SESSION', False):
            logging.info("[_execute_start_program] auto_run_program setting is true for direct call, adding 1s delay.")
            time.sleep(1)

        logging.info(f"[_execute_start_program] Proceeding to start runner_instance (ID: {id(runner_instance)}).")
        # Pass the is_restart parameter to the runner's start method
        threading.Thread(target=lambda: runner_instance.start(is_restart=is_restart)).start()
        current_app.config['PROGRAM_RUNNING'] = True
        
        global program_runner # module-level global in this file
        program_runner = runner_instance

        logging.info(f"[_execute_start_program] runner_instance (ID: {id(runner_instance)}) start initiated. Set app.config['PROGRAM_RUNNING']=True.")
        send_queue_start_notification("Queue processing started") # Consider context: "via UI" or "automatically"
        logging.info("[_execute_start_program] Sent queue start notification.")

        # If we skipped the connectivity check, trigger an immediate post-start check so
        # that the ProgramRunner can pause itself if services are unavailable.
        if skip_connectivity_check:
            try:
                logging.info("[_execute_start_program] Triggering post-start connectivity check via task_check_service_connectivity (skip_connectivity_check=True)")
                # Direct call – this will invoke normal failure-handling logic.
                runner_instance.task_check_service_connectivity()
            except Exception as e_post_chk:
                logging.error(f"[_execute_start_program] Post-start connectivity check failed: {e_post_chk}", exc_info=True)

        logging.info("--- _execute_start_program completing successfully ---")
        return {"status": "success", "message": "Program started successfully"}
    finally:
        if _program_op_lock.locked(): # Ensure it was acquired by this thread before releasing
             _program_op_lock.release()
# --- END EDIT ---

@program_operation_bp.route('/api/start_program', methods=['POST'])
@admin_required # This decorator remains for HTTP API calls
def start_program():
    # --- START EDIT: Delegate to the new internal function ---
    logging.info("--- /api/start_program HTTP endpoint called, delegating to _execute_start_program ---")
    # _execute_start_program uses current_app.program_runner which is set up in main.py
    # The Flask app context is active here.
    # Check if this is a restart request from the request data
    is_restart = False
    try:
        if request.is_json and request.json:
            is_restart = request.json.get('is_restart', False)
    except Exception as e:
        logging.warning(f"Could not parse request JSON: {e}")
    
    result = _execute_start_program(is_restart=is_restart)
    
    # Determine HTTP status code based on result
    http_status_code = 200
    if result.get("status") == "error":
        message = result.get("message", "").lower()
        if "operation is in progress" in message or \
           "currently stopping" in message:
            http_status_code = 409 # Conflict
        elif "scheduler listeners" in message or \
             "not initialized in application context" in message or \
             "failed to connect to required services" in message: # Added connectivity check
            http_status_code = 500 # Internal Server Error for critical issues
        # Other errors might keep 200 if they are logical (e.g. bad input for other routes)
        # but for start_program, most errors imply a server-side issue or conflict.

    logging.info(f"--- /api/start_program HTTP endpoint returning jsonify(result) with status {http_status_code} ---")
    if result.get('status') != 'error':
        try:
            from flask_login import current_user as _cu
            from utilities.ai_habits import track_action
            _uid = _cu.username if _cu.is_authenticated else 'system'
            track_action('program_start' if not is_restart else 'program_restart', user_id=_uid)
        except Exception:
            pass
    return jsonify(result), http_status_code
    # --- END EDIT ---

def stop_program():
    if not _program_op_lock.acquire(blocking=False):
        logging.warning("[stop_program] Could not acquire lock, another operation in progress.")
        return {"status": "error", "message": "System busy: Another start/stop operation is in progress. Please try again shortly."}

    try:
        logging.info("--- Entered internal stop_program function ---")
        
        app_runner_instance = getattr(current_app, 'program_runner', None)
        global program_runner # module-level global in this file
        
        if app_runner_instance is not None:
            logging.info(f"[stop_program] Found app_runner_instance (ID: {id(app_runner_instance)}). Is_running: {app_runner_instance.is_running()}, Is_initializing: {app_runner_instance.is_initializing()}, Is_stopping: {hasattr(app_runner_instance, 'is_stopping') and app_runner_instance.is_stopping()}")
            
            if hasattr(app_runner_instance, 'is_initializing') and app_runner_instance.is_initializing():
                logging.warning(f"[stop_program] Attempt to stop while program is initializing (runner ID: {id(app_runner_instance)}).")
                return {"status": "error", "message": "Program is currently initializing. Cannot stop at this moment."}

            # If it's already stopped (is_running is false and not currently in the process of stopping)
            if not app_runner_instance.is_running() and (not hasattr(app_runner_instance, 'is_stopping') or not app_runner_instance.is_stopping()):
                logging.info(f"[stop_program] Program (runner ID: {id(app_runner_instance)}) appears to be already stopped.")
                # Ensure cleanup is still performed if this function is called on an already stopped instance
                # but the main message should reflect it was already stopped.
                # The existing logic below will handle nullifying ProgramRunner._instance etc.

            if app_runner_instance.is_running() or (hasattr(app_runner_instance, 'is_stopping') and app_runner_instance.is_stopping()):
                if app_runner_instance.is_running(): # Only log stopping if it was actually running
                    logging.info(f"[stop_program] Stopping app_runner_instance (ID: {id(app_runner_instance)}).")
                send_queue_stop_notification("Queue processing stopped.") # Send regardless if stopping or was running
                app_runner_instance.stop() # This should call scheduler.shutdown() and set its internal _stopping flag
                logging.info(f"[stop_program] app_runner_instance (ID: {id(app_runner_instance)}) stop() method called.")
            
            # Mark for re-initialization and clear scheduler
            app_runner_instance.initial_listeners_setup_complete = False
            logging.info(f"[stop_program] Set initial_listeners_setup_complete=False for app_runner_instance (ID: {id(app_runner_instance)}).")
            
            # Ensure the scheduler is None so a new one is created on restart
            if hasattr(app_runner_instance, 'scheduler'):
                # Ensure it's shut down if stop() didn't (though it should)
                if app_runner_instance.scheduler is not None and app_runner_instance.scheduler.running:
                    logging.warning(f"[stop_program] app_runner_instance scheduler was still running after stop(). Shutting down now.")
                    app_runner_instance.scheduler.shutdown(wait=False)
                app_runner_instance.scheduler = None 
                logging.info(f"[stop_program] Set app_runner_instance.scheduler=None (ID: {id(app_runner_instance)}).")
        else:
            logging.info("[stop_program] current_app.program_runner was None. Program likely already stopped or not started.")
            # If there's no runner instance, it's effectively stopped.
            # Still good to nullify the module-level one and the singleton if they exist.

        # Nullify the module-level 'program_runner' in this file
        if program_runner is not None:
            if program_runner is app_runner_instance:
                logging.info("[stop_program] Module-level program_runner was same as app_runner_instance.")
            else: # Different instance, attempt to stop it too if it's a ProgramRunner
                logging.info(f"[stop_program] Module-level program_runner (ID: {id(program_runner)}) is different. Attempting cleanup if it's a ProgramRunner.")
                if isinstance(program_runner, ProgramRunner):
                     if program_runner.is_running(): # Check if it was running before stopping
                         program_runner.stop()
                     program_runner.initial_listeners_setup_complete = False
                     if hasattr(program_runner, 'scheduler'):
                          program_runner.scheduler = None

            program_runner = None
            logging.info("[stop_program] Set module-level program_runner to None.")

        if hasattr(ProgramRunner, '_instance'):
            ProgramRunner._instance = None
            logging.info("[stop_program] Reset ProgramRunner._instance singleton to None.")

        current_app.config['PROGRAM_RUNNING'] = False
        logging.info("[stop_program] Set current_app.config['PROGRAM_RUNNING'] = False.")
        logging.info("--- internal stop_program function completed ---")
        return {"status": "success", "message": "Program stopped successfully."}
    finally:
        if _program_op_lock.locked(): # Ensure it was acquired by this thread before releasing
            _program_op_lock.release()

@program_operation_bp.route('/api/stop_program', methods=['POST'])
@admin_required
def stop_program_route():
    logging.info("--- /api/stop_program HTTP endpoint called ---")
    result = stop_program()
    http_status_code = 200
    if result.get("status") == "error":
        message = result.get("message", "").lower()
        if "operation is in progress" in message or \
           "currently initializing" in message:
            http_status_code = 409 # Conflict
        # Other errors could be 500 if critical, but stop tends to succeed or be a conflict.
    
    logging.info(f"--- /api/stop_program HTTP endpoint returning: {result} with status {http_status_code} ---")
    if result.get('status') != 'error':
        try:
            from flask_login import current_user as _cu
            from utilities.ai_habits import track_action
            _uid = _cu.username if _cu.is_authenticated else 'system'
            track_action('program_stop', user_id=_uid)
        except Exception:
            pass
    return jsonify(result), http_status_code

@program_operation_bp.route('/api/update_program_state', methods=['POST'])
@admin_required
def update_program_state():
    data = request.json
    action = data.get('action')
    if action in ['Running', 'Initialized']:
        current_app.config['PROGRAM_RUNNING'] = (action == 'Running')
        return jsonify({"status": "success", "message": f"Program state updated to {action}"})
    else:
        return jsonify({"status": "error", "message": "Invalid state"}), 400

@program_operation_bp.route('/api/program_status', methods=['GET'])
def program_status():
    """
    Returns the current operational status of the program.
    This provides a single string ('Stopped', 'Starting', 'Running', 'Stopping')
    for streamlined frontend state management.
    """
    # get_program_status() is a new helper that uses the get_status() method on the runner
    status = get_program_status() 
    return jsonify({"status": status})

def program_is_running():
    runner = get_program_runner()
    return runner.is_running() if runner else False

def get_program_status():
    """
    Helper function to get the detailed program status.
    This can be used by template context processors.
    """
    runner = get_program_runner()
    if runner and hasattr(runner, 'get_status'):
        return runner.get_status()
    # Fallback for when runner is not yet available
    from flask import current_app
    if current_app.config.get('PROGRAM_RUNNING'):
        return "Running"
    return "Stopped"

@program_operation_bp.route('/api/check_program_conditions')
@admin_required
def check_program_conditions():
    config = load_config()
    scrapers_enabled = any(scraper.get('enabled', False) for scraper in config.get('Scrapers', {}).values())
    # Consider content sources present (ignoring per-source enabled flags); UI toggles control runtime
    content_sources_enabled = bool(config.get('Content Sources', {}))
    
    required_settings = [
        ('Plex', 'url'),
        ('Plex', 'token'),
        ('Metadata Battery', 'url')
    ]

    missing_fields = []
    for category, key in required_settings:
        value = get_setting(category, key)
        if not value:
            missing_fields.append(f"{category}.{key}")

    # Require at least one of: Debrid Provider OR Usenet Provider (cli_mount)
    debrid_ok = bool(get_setting('Debrid Provider', 'provider') and get_setting('Debrid Provider', 'api_key'))
    usenet_ok = bool(get_setting('Usenet Provider', 'enabled') and get_setting('Usenet Provider', 'url'))
    if not debrid_ok and not usenet_ok:
        missing_fields.append("Debrid Provider or Usenet Provider")

    required_settings_complete = len(missing_fields) == 0

    return jsonify({
        'canRun': scrapers_enabled and content_sources_enabled and required_settings_complete,
        'scrapersEnabled': scrapers_enabled,
        'contentSourcesEnabled': content_sources_enabled,
        'requiredSettingsComplete': required_settings_complete,
        'missingFields': missing_fields
    })

@program_operation_bp.route('/api/task_timings', methods=['GET'])
@admin_required
def get_task_timings():
    runner = get_program_runner()
    if not runner or not runner.is_running():
        # If not running, return empty or maybe load saved toggles/intervals?
        # Let's return empty for now, UI can handle loading saved states.
        return jsonify(success=False, error="Program is not running", tasks={
            'queues': {}, 'content_sources': {}, 'system_tasks': {},
            'library_tasks': {}, 'metadata_tasks': {}, 'feature_tasks': {}
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

    # Tasks explicitly assigned to Library, Metadata, or Feature tabs
    _LIBRARY_TASKS = {
        'task_verify_plex_removals',
        'task_check_trakt_early_releases',
        'task_refresh_library_size_cache',
        'task_check_plex_files',
        'task_plex_full_scan',
        'task_verify_symlinked_files',
        'task_get_plex_watch_history',
        'task_run_library_maintenance',
        'task_analyze_media_files',
        'task_manual_plex_full_scan',
        'task_sync_library_metadata',
        'task_backfill_plex_guids',
        'task_backfill_plex_ms_item_id',
        'task_sync_cli_mount_changes',
        'task_repair_broken_nzbs',
        'task_repair_broken_debrids',
    }
    _METADATA_TASKS = {
        'task_refresh_release_dates',
        'task_sync_episode_metadata',
        'task_update_show_ids',
        'task_update_show_titles',
        'task_update_movie_ids',
        'task_update_movie_titles',
        'task_update_tv_show_status',
        'task_cleanup_title_year_suffixes',
    }
    _FEATURE_TASKS = {
        'task_overlay_sync',
        'task_overlay_cleanup',
        'task_sync_plex_labels',
        'task_process_bulk_subtitles',
        'task_backup_debrid',
        'task_cleanup_debrid',
        'task_upgrade_hub_scan',
        'task_upgrade_hub_auto_queue',
        'task_plex_smart_collection_posters',
        'task_plex_movie_boxsets',
        'task_backfill_nzb_torrent_ids',
    }

    tasks_data = {
        'queues': {},
        'content_sources': {},
        'system_tasks': {},
        'library_tasks': {},
        'metadata_tasks': {},
        'feature_tasks': {},
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

        # The configured interval is the custom value if set, otherwise the default
        configured_interval = task_custom_saved_interval if task_custom_saved_interval is not None else task_default_interval
        
        task_info = {
            'enabled': job is not None and job.next_run_time is not None,
            'interval': 0, # Current interval from job or default
            'next_run_in': {'hours': 0, 'minutes': 0, 'seconds': 0, 'total_seconds': 0},
            'display_name': _format_task_display_name(normalized_name, queue_map, content_sources_map),
            'current_interval_seconds': 0, # Actual live interval or configured if disabled
            'default_interval_seconds': task_default_interval, # Original default (never changes)
            'custom_interval_seconds': task_custom_saved_interval, # Raw saved value (number or null)
            'configured_interval_seconds': configured_interval # Current effective interval (custom or default)
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
                 simple_key_match = source_key in content_sources_map
                 if simple_key_match:
                      is_content_source = True

             if is_content_source:
                 tasks_data['content_sources'][normalized_name] = task_info
             else:
                 logging.debug(f"Task '{normalized_name}' looks like a content source but key '{source_key}' not found in content_sources map. Categorizing as system.")
                 tasks_data['system_tasks'][normalized_name] = task_info
        elif normalized_name in _LIBRARY_TASKS:
            tasks_data['library_tasks'][normalized_name] = task_info
        elif normalized_name in _METADATA_TASKS:
            tasks_data['metadata_tasks'][normalized_name] = task_info
        elif normalized_name in _FEATURE_TASKS:
            tasks_data['feature_tasks'][normalized_name] = task_info
        else:
            tasks_data['system_tasks'][normalized_name] = task_info

    return jsonify(success=True, tasks=tasks_data)

@program_operation_bp.route('/task_timings')
@admin_required
def task_timings():
    return render_template('task_timings.html')

@program_operation_bp.route('/trigger_task', methods=['POST'])
@admin_required
def trigger_task():
    """Manually trigger a task by adding it to the APScheduler queue."""
    task_name = request.form.get('task_name')
    if not task_name:
        # flash('Task name not provided.', 'error')
        # return redirect(url_for('program_operation.task_timings'))
        return jsonify({'success': False, 'message': 'Task name not provided.'}), 400

    runner = get_program_runner()
    if not runner:
        # flash('ProgramRunner not initialized. Cannot trigger task.', 'error')
        # return redirect(url_for('program_operation.task_timings'))
        return jsonify({'success': False, 'message': 'ProgramRunner not initialized. Cannot trigger task.'}), 500

    try:
        # trigger_task now returns a dict or raises an exception
        result = runner.trigger_task(task_name)
        
        # result will be like {"success": True, "message": "Task 'X' queued...", "job_id": "manual_X_uuid"}
        # We use flash messages for this route as it's typically form submissions from task_timings.html
        # flash(f"{result.get('message', 'Task action initiated.')} (Job ID: {result.get('job_id', 'N/A')})", 'success')
        return jsonify(result) # Return the result from runner.trigger_task directly
    except ValueError as ve:
        # flash(f"Error triggering task '{task_name}': {str(ve)}", 'error')
        return jsonify({'success': False, 'message': f"Error triggering task '{task_name}': {str(ve)}"}), 400
    except RuntimeError as re:
        # flash(f"Failed to queue task '{task_name}': {str(re)}", 'error')
        return jsonify({'success': False, 'message': f"Failed to queue task '{task_name}': {str(re)}"}), 500
    except Exception as e:
        logging.error(f"Unexpected error triggering task {task_name}: {e}", exc_info=True)
        # flash(f"An unexpected error occurred while triggering task '{task_name}'.", 'error')
        return jsonify({'success': False, 'message': f"An unexpected error occurred while triggering task '{task_name}'."}), 500
        
    # return redirect(url_for('program_operation.task_timings')) # Old redirect

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
    """Save the current state of task toggles to a JSON file, based on ProgramRunner's live state."""
    MIGRATION_VERSION_KEY = "_migration_version"
    runner = get_program_runner()

    if not runner:
        return jsonify({'success': False, 'error': 'ProgramRunner not available. Cannot determine live task states.'}), 500

    try:
        # Construct live_task_states from ProgramRunner
        live_task_states = {}
        if hasattr(runner, 'task_intervals') and hasattr(runner, 'enabled_tasks'):
            all_defined_tasks = runner.task_intervals.keys()
            for task_name in all_defined_tasks:
                # Ensure we use the same normalized name that would be stored/checked
                normalized_name = runner._normalize_task_name(task_name) 
                live_task_states[normalized_name] = normalized_name in runner.enabled_tasks
        else:
            return jsonify({'success': False, 'error': 'ProgramRunner instance is missing necessary attributes (task_intervals or enabled_tasks).'}), 500

        # Save the full live state for all known tasks.
        # Diff-based saving caused a bug: after one correct restart with a disabled task,
        # the snapshot no longer contained that task (since it was already excluded by the
        # toggle-aware init logic), so subsequent saves dropped the 'false' entry, causing
        # the task to re-enable on the next restart.
        # Saving the full state is safe: stale tasks are excluded because we only iterate
        # runner.task_intervals.keys() above, and the file format is unchanged.
        data_to_save = live_task_states.copy()

        # Get the file path
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        toggles_file_path = os.path.join(db_content_dir, 'task_toggles.json')

        migration_version = None

        # Read existing file to preserve metadata (like _migration_version)
        if os.path.exists(toggles_file_path):
            try:
                with open(toggles_file_path, 'r') as f:
                    current_data_from_file = json.load(f)
                    if isinstance(current_data_from_file, dict) and MIGRATION_VERSION_KEY in current_data_from_file:
                        migration_version = current_data_from_file[MIGRATION_VERSION_KEY]
            except (json.JSONDecodeError, OSError) as e:
                logging.warning(f"Could not read existing task toggles file to preserve metadata: {e}")

        # Preserve the migration version marker
        if migration_version is not None:
            data_to_save[MIGRATION_VERSION_KEY] = migration_version

        # Save the combined data to JSON file
        try:
            os.makedirs(os.path.dirname(toggles_file_path), exist_ok=True)
            with open(toggles_file_path, 'w') as f:
                json.dump(data_to_save, f, indent=4)
            logging.info(f"Live task toggle states saved to {toggles_file_path}")
            return jsonify({'success': True, 'message': 'Task toggle states saved successfully based on live program state.'})
        except OSError as e:
             logging.error(f"Error writing task toggles file: {str(e)}")
             return jsonify({'success': False, 'error': f"Failed to write file: {str(e)}"})

    except Exception as e:
        logging.error(f"Error saving task toggles: {str(e)}", exc_info=True)
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
    MIN_INTERVAL_SECONDS = 1

    for task_name, interval_seconds_str in custom_intervals_seconds_input.items():
        if interval_seconds_str is None or interval_seconds_str == '':
             valid_intervals_seconds[task_name] = None # Reset to default / Use default
             continue

        try:
            interval_sec = int(interval_seconds_str)
            if interval_sec >= MIN_INTERVAL_SECONDS:
                 valid_intervals_seconds[task_name] = interval_sec
            else:
                 errors.append(f"Invalid interval for {task_name}: must be {MIN_INTERVAL_SECONDS} seconds or greater.")
        except (ValueError, TypeError):
            errors.append(f"Invalid interval format for {task_name}: '{interval_seconds_str}' is not a whole number.")

    if errors:
        return jsonify(success=False, error="Validation errors: " + "; ".join(errors)), 400

    intervals_file_path = _get_task_intervals_file_path()
    try:
        os.makedirs(os.path.dirname(intervals_file_path), exist_ok=True)

        existing_intervals = {}
        if os.path.exists(intervals_file_path):
             try:
                  with open(intervals_file_path, 'r') as f:
                       existing_intervals = json.load(f)
             except json.JSONDecodeError:
                  logging.warning(f"Could not decode existing intervals file {intervals_file_path}. It will be treated as empty.")
                  existing_intervals = {}

        final_intervals_to_save = existing_intervals.copy()
        
        # Process the validated intervals from the current request
        # valid_intervals_seconds contains {task_name: value} or {task_name: None}
        for task_name, interval_val in valid_intervals_seconds.items():
            if interval_val is None:
                # Frontend indicated this task should use its default interval
                # So, remove it from our map of custom intervals if it's there.
                if task_name in final_intervals_to_save:
                    del final_intervals_to_save[task_name]
            else:
                # Frontend sent a specific custom value (it's guaranteed not to be the default)
                # So, save this custom value.
                final_intervals_to_save[task_name] = interval_val

        with open(intervals_file_path, 'w') as f:
            json.dump(final_intervals_to_save, f, indent=4)

        runner = get_program_runner()
        updated_live = 0
        update_errors = []
        if runner and runner.is_running():
            logging.info("Program running, attempting to apply interval changes (seconds) live...")
            # Iterate over valid_intervals_seconds because it reflects the user's latest batch of changes
            for task_name, interval_sec_or_none in valid_intervals_seconds.items():
                # Apply live interval change only if the task is currently enabled to avoid re-enabling paused tasks
                normalized_name = runner._normalize_task_name(task_name) if hasattr(runner, '_normalize_task_name') else task_name
                task_currently_enabled = hasattr(runner, 'enabled_tasks') and normalized_name in runner.enabled_tasks

                if not task_currently_enabled:
                    # Skip live update; interval will take effect next time the task is enabled or on program restart
                    logging.info(f"Skipping live interval update for disabled task '{normalized_name}'. Interval preference stored only.")
                    continue

                if hasattr(runner, 'update_task_interval'):
                    try:
                        if runner.update_task_interval(task_name, interval_sec_or_none): # Pass processed value (int or None)
                             updated_live += 1
                    except Exception as live_e:
                        update_errors.append(f"Error applying live update for {task_name}: {live_e}")
                else:
                     update_errors.append("ProgramRunner does not support live interval updates.")
                     break # Stop trying if method not found
            if update_errors:
                 logging.warning("Some live interval updates failed: " + "; ".join(update_errors))
        
        save_message = "Task intervals processed. "
        if updated_live > 0:
            save_message += f"{updated_live} changes applied live. "
        save_message += "Remaining changes (if any) or resets will apply on program restart."
        
        return jsonify(success=True, message=save_message)

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
        # Special-case display names for specific queue tasks
        if task_name == 'final_check_queue':
            return 'Final Scrape'
        elif task_name == 'Pre_release':
            return 'Pre-Release'
        # For other queues, the key itself is usually descriptive enough
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
             logging.debug(f"Could not find source key '{source_key}' in content_sources_map for display name formatting.")
             # Fallback to generic formatting
             display_name = source_key.replace('_', ' ').strip()
             return ' '.join(word.capitalize() for word in display_name.split()) + " (Source)"


    # Handle other system tasks (usually start with 'task_')
    if task_name.startswith('task_'):
        _TASK_DISPLAY_OVERRIDES = {
            'task_sync_cli_mount_changes': 'Sync cli_mount Changes',
        }
        if task_name in _TASK_DISPLAY_OVERRIDES:
            return _TASK_DISPLAY_OVERRIDES[task_name]
        display_name = task_name[5:].replace('_', ' ').strip() # Remove prefix, replace underscores
         # Capitalize first letter of each word
        return ' '.join(word.capitalize() for word in display_name.split())

    # Default fallback if no rule matches (should be rare)
    return task_name.replace('_', ' ').capitalize()
# --- END EDIT ---


# ---------------------------------------------------------------------------
# NzbDAV setup helper — backs the in-app "Test connection & ensure categories"
# button (Settings → Usenet Provider and onboarding). NzbDAV exposes:
#   GET  /api?mode=version            reachability (no key needed)
#   GET  /api?mode=get_cats&apikey=   the SAB category list cli-debrid sees
#   POST /api/get-config              read config  (form field config-keys)
#   POST /api/update-config           write config (form field config=JSON);
#                                     applied live via ConfigManager — no restart
# The config API is reachable without the SAB key when NzbDAV runs with
# DISABLE_FRONTEND_AUTH=true (the documented cli-debrid setup). If it is gated by
# the frontend login we report that categories must be added in the NzbDAV UI.
# ---------------------------------------------------------------------------

# Categories cli-debrid's title heuristic routes grabs into; these must exist in
# NzbDAV or submits are rejected (items loop in "Wanted"). Mirrors nzbdav_migrate.py.
# Fallback only — the live list is derived from usenet.nzbdav_client (the same
# source the submit-router and repair scope use), see _nzbdav_required_categories.
NZBDAV_REQUIRED_CATEGORIES = ['movies', 'shows', 'movies_1080p', 'shows_1080p', '__unplayable__']
NZBDAV_RECOMMENDED_CATEGORIES = ['music']


def _nzbdav_base_url(raw):
    """Normalise a user-entered NzbDAV URL to its API root (no trailing slash, no /api)."""
    url = (raw or '').strip().rstrip('/')
    if url.endswith('/api'):
        url = url[:-4].rstrip('/')
    return url


def _nzbdav_required_categories(download_folder=''):
    """Categories cli-debrid needs on the NzbDAV instance.

    Derived from the SAME source as the submit-router and repair scope
    (usenet.nzbdav_client.managed_categories + the optional category map), so the
    setup helper can never tell the user to create a set that disagrees with what
    cli-debrid actually uploads to — the historical cause of invisible items.
    """
    try:
        from usenet.nzbdav_client import _parse_category_map, managed_categories
        cat_map = _parse_category_map(get_setting('Usenet Provider', 'nzbdav_category_map', ''))
        cats = sorted(managed_categories(cat_map))
    except Exception:
        cats = list(NZBDAV_REQUIRED_CATEGORIES)
    df = (download_folder or '').strip()
    if df and df not in cats:
        cats.append(df)
    return cats


def _nzbdav_get_categories_via_config(base_url, timeout=10):
    """Read api.categories through NzbDAV's config API.
    Returns (categories_list | None, config_api_reachable_bool)."""
    import requests
    try:
        r = requests.post(f"{base_url}/api/get-config",
                          files={'config-keys': (None, 'api.categories')}, timeout=timeout)
        if r.status_code != 200:
            return None, False
        data = r.json()
        if not data.get('status'):
            return None, False
        for item in data.get('configItems', []):
            if item.get('configName') == 'api.categories':
                return [c.strip() for c in (item.get('configValue') or '').split(',') if c.strip()], True
        return [], True
    except (RequestException, ValueError):
        return None, False


@program_operation_bp.route('/api/nzbdav/check', methods=['POST'])
@admin_required
def nzbdav_check():
    """Probe an NzbDAV instance for the in-app setup helper: reachability, version,
    SAB key validity, and which categories cli-debrid needs are missing."""
    import requests
    payload = request.get_json(silent=True) or request.form
    url = _nzbdav_base_url(payload.get('url') or get_setting('Usenet Provider', 'url', ''))
    token = (payload.get('api_token') or get_setting('Usenet Provider', 'api_token', '') or '').strip()
    download_folder = payload.get('download_folder') or get_setting('Usenet Provider', 'download_folder', '')
    required = _nzbdav_required_categories(download_folder)

    result = {'url': url, 'reachable': False, 'version': None, 'key_valid': None,
              'categories_present': [], 'categories_missing': [], 'recommended_missing': [],
              'can_autofix': False, 'required': required,
              'recommended': NZBDAV_RECOMMENDED_CATEGORIES}
    if not url:
        return jsonify({'success': False, 'error': 'No NzbDAV URL provided.'}), 400

    # 1) reachability + version (SAB mode=version, no key needed)
    try:
        r = requests.get(f"{url}/api", params={'mode': 'version'}, timeout=10)
        if r.status_code == 200:
            try:
                result['version'] = r.json().get('version')
            except ValueError:
                result['version'] = None
            result['reachable'] = True
    except RequestException as e:
        return jsonify({'success': False, 'error': f'Cannot reach NzbDAV at {url}: {e}', 'result': result}), 200

    if not result['reachable']:
        return jsonify({'success': False,
                        'error': f'NzbDAV at {url} did not respond to mode=version.',
                        'result': result}), 200

    # 2) categories cli-debrid sees via SAB get_cats (uses the key) + key validity
    present = None
    try:
        r = requests.get(f"{url}/api", params={'mode': 'get_cats', 'apikey': token}, timeout=10)
        if r.status_code == 200:
            cats = (r.json() or {}).get('categories')
            if isinstance(cats, list):
                present = [c if isinstance(c, str) else c.get('name', '') for c in cats]
                result['key_valid'] = True
            else:
                result['key_valid'] = False
        else:
            result['key_valid'] = False
    except (RequestException, ValueError):
        result['key_valid'] = False

    # Config API gives the authoritative list and tells us whether auto-fix is possible
    cfg_cats, cfg_reachable = _nzbdav_get_categories_via_config(url)
    result['can_autofix'] = cfg_reachable
    if present is None and cfg_cats is not None:
        present = cfg_cats

    present = present or []
    result['categories_present'] = present
    result['categories_missing'] = [c for c in required if c not in present]
    result['recommended_missing'] = [c for c in NZBDAV_RECOMMENDED_CATEGORIES if c not in present]
    return jsonify({'success': True, 'result': result}), 200


@program_operation_bp.route('/api/nzbdav/ensure_categories', methods=['POST'])
@admin_required
def nzbdav_ensure_categories():
    """Create any missing cli-debrid categories in NzbDAV via its config API
    (read api.categories, append the missing ones, write back). Applied live."""
    import requests
    payload = request.get_json(silent=True) or request.form
    url = _nzbdav_base_url(payload.get('url') or get_setting('Usenet Provider', 'url', ''))
    download_folder = payload.get('download_folder') or get_setting('Usenet Provider', 'download_folder', '')
    include_recommended = str(payload.get('include_recommended', 'true')).lower() in ('1', 'true', 'yes', 'on')
    required = _nzbdav_required_categories(download_folder)
    if include_recommended:
        required = required + [c for c in NZBDAV_RECOMMENDED_CATEGORIES if c not in required]

    if not url:
        return jsonify({'success': False, 'error': 'No NzbDAV URL provided.'}), 400

    current, reachable = _nzbdav_get_categories_via_config(url)
    if not reachable or current is None:
        return jsonify({'success': False,
                        'error': "NzbDAV config API not reachable. If NzbDAV runs with frontend "
                                 "authentication enabled, add the categories manually in "
                                 "NzbDAV → Settings → Categories, or set DISABLE_FRONTEND_AUTH=true."}), 200

    to_add = [c for c in required if c not in current]
    if not to_add:
        return jsonify({'success': True, 'created': [], 'all_present': True, 'categories': current}), 200

    new_list = current + to_add
    try:
        r = requests.post(f"{url}/api/update-config",
                          files={'config': (None, json.dumps({'api.categories': ','.join(new_list)}))},
                          timeout=15)
        if r.status_code != 200 or not (r.json() or {}).get('status', False):
            return jsonify({'success': False,
                            'error': f'update-config failed: HTTP {r.status_code} {r.text[:200]}'}), 200
    except (RequestException, ValueError) as e:
        return jsonify({'success': False, 'error': f'update-config error: {e}'}), 200

    verify, _ = _nzbdav_get_categories_via_config(url)
    verify = verify or new_list
    still_missing = [c for c in required if c not in verify]
    return jsonify({'success': len(still_missing) == 0, 'created': to_add,
                    'all_present': len(still_missing) == 0, 'still_missing': still_missing,
                    'categories': verify}), 200


# ---------------------------------------------------------------------------
# Provider transfer — migrate downloads between usenet backends (Debug -> Library)
# Engine lives in utilities/provider_transfer.py (HTTP/mounted-path, no docker).
# ---------------------------------------------------------------------------

@program_operation_bp.route('/api/provider_transfer/config', methods=['GET'])
@admin_required
def provider_transfer_config():
    """The configured Usenet Provider, used to prefill the transfer UI (it lives
    in Debug -> Library where the settings context isn't available)."""
    data_path = get_setting('Usenet Provider', 'data_path', '') or '/climount_data'
    return jsonify({
        'provider': get_setting('Usenet Provider', 'provider', '') or '',
        'url': get_setting('Usenet Provider', 'url', '') or '',
        'token': get_setting('Usenet Provider', 'api_token', '') or '',
        'data_path': data_path,
        'nzb_path_guess': data_path.rstrip('/') + '/usenet/nzbs',
    }), 200


@program_operation_bp.route('/api/provider_transfer/list', methods=['POST'])
@admin_required
def provider_transfer_list():
    """Preview the source: how many items are transferable + a sample of names."""
    from utilities import provider_transfer
    p = request.get_json(silent=True) or request.form
    items, err = provider_transfer.list_source_items(
        p.get('direction', ''), p.get('source_url', ''), p.get('source_token', ''),
        p.get('source_nzb_path', ''))
    if err:
        return jsonify({'success': False, 'error': err}), 200
    return jsonify({'success': True, 'count': len(items),
                    'sample': [it['name'] for it in items[:50]]}), 200


@program_operation_bp.route('/api/provider_transfer/start', methods=['POST'])
@admin_required
def provider_transfer_start():
    """Kick off a background transfer job. Returns a job_id to poll."""
    from utilities import provider_transfer
    p = request.get_json(silent=True) or request.form
    if p.get('direction') not in ('climount_to_nzbdav', 'nzbdav_to_climount'):
        return jsonify({'success': False, 'error': 'invalid direction'}), 400
    if not (p.get('target_url') or '').strip():
        return jsonify({'success': False, 'error': 'target URL required'}), 400
    job_id = provider_transfer.start_job({
        'direction': p.get('direction'),
        'source_url': p.get('source_url', ''), 'source_token': p.get('source_token', ''),
        'source_nzb_path': p.get('source_nzb_path', ''),
        'target_url': p.get('target_url', ''), 'target_token': p.get('target_token', ''),
        'target_category': p.get('target_category', ''),
        'skip_existing': p.get('skip_existing', True),
        'max_queue': p.get('max_queue', 3), 'limit': p.get('limit', 0),
    })
    return jsonify({'success': True, 'job_id': job_id}), 202


@program_operation_bp.route('/api/provider_transfer/status/<job_id>', methods=['GET'])
@admin_required
def provider_transfer_status(job_id):
    from utilities import provider_transfer
    st = provider_transfer.get_status(job_id)
    if not st:
        return jsonify({'success': False, 'error': 'unknown job'}), 404
    return jsonify({'success': True, 'status': st}), 200


@program_operation_bp.route('/api/provider_transfer/cancel/<job_id>', methods=['POST'])
@admin_required
def provider_transfer_cancel(job_id):
    from utilities import provider_transfer
    return jsonify({'success': provider_transfer.cancel_job(job_id)}), 200
