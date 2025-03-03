import sys
import os
import appdirs
import threading
import time
import signal
import logging
import platform
import psutil
import webbrowser
import socket
import sqlite3
from datetime import datetime

# Import Windows-specific modules only on Windows
if platform.system() == 'Windows':
    import win32gui
    import win32con

# Existing imports
import shutil
import requests
import re
import subprocess
from settings import set_setting
from settings import get_setting
from logging_config import stop_global_profiling, start_global_profiling
import babelfish
from content_checkers.plex_watchlist import validate_plex_tokens
from notifications import (
    setup_crash_handler, 
    register_shutdown_handler, 
    register_startup_handler,
    send_program_stop_notification
)

if sys.platform.startswith('win'):
    app_name = "cli_debrid"  # Replace with your app's name
    app_author = "cli_debrid"  # Replace with your company name
    base_path = appdirs.user_data_dir(app_name, app_author)
    os.environ['USER_CONFIG'] = os.path.join(base_path, 'config')
    os.environ['USER_LOGS'] = os.path.join(base_path, 'logs')
    os.environ['USER_DB_CONTENT'] = os.path.join(base_path, 'db_content')
else:
    os.environ.setdefault('USER_CONFIG', '/user/config')
    os.environ.setdefault('USER_LOGS', '/user/logs')
    os.environ.setdefault('USER_DB_CONTENT', '/user/db_content')

# Ensure directories exist
for dir_path in [os.environ['USER_CONFIG'], os.environ['USER_LOGS'], os.environ['USER_DB_CONTENT']]:
    os.makedirs(dir_path, exist_ok=True)

print(f"USER_CONFIG: {os.environ['USER_CONFIG']}")
print(f"USER_LOGS: {os.environ['USER_LOGS']}")
print(f"USER_DB_CONTENT: {os.environ['USER_DB_CONTENT']}")

import logging
import shutil
import signal
import time
from api_tracker import api
from settings import get_setting
import requests
import re
from settings import set_setting
import subprocess
import threading
from logging_config import stop_global_profiling, start_global_profiling
import babelfish

# Global variables
program_runner = None
metadata_process = None
metadata_lock = threading.Lock()

def get_babelfish_data_dir():
    return os.path.join(os.path.dirname(babelfish.__file__), 'data')

def setup_logging():
    logging.getLogger('selector').setLevel(logging.WARNING)
    logging.getLogger('asyncio').setLevel(logging.WARNING)

    # Get log directory from environment variable with fallback
    log_dir = os.environ.get('USER_LOGS', '/user/logs')

    # Ensure logs directory exists
    os.makedirs(log_dir, exist_ok=True)

    # Ensure log files exist
    for log_file in ['debug.log']:
        log_path = os.path.join(log_dir, log_file)
        if not os.path.exists(log_path):
            open(log_path, 'a').close()

    import logging_config
    logging_config.setup_logging()

def setup_directories():
    # Get config directory from environment variable
    config_dir = os.environ.get('USER_CONFIG', '/user/config')
    log_dir = os.environ.get('USER_LOGS', '/user/logs')
    db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    
    # Ensure directories exist
    os.makedirs(config_dir, exist_ok=True)
    os.makedirs(log_dir, exist_ok=True)
    os.makedirs(db_content_dir, exist_ok=True)

def backup_config():
    # Get config directory from environment variable with fallback
    config_dir = os.environ.get('USER_CONFIG', '/user/config')
    config_path = os.path.join(config_dir, 'config.json')
    if os.path.exists(config_path):
        backup_path = os.path.join(config_dir, 'config_backup.json')
        shutil.copy2(config_path, backup_path)
        logging.info(f"Backup of config.json created: {backup_path}")
    else:
        logging.warning("config.json not found, no backup created.")

def backup_database():
    """
    Creates a backup of the media_items.db file with a timestamp.
    Keeps only the two most recent backups.
    """
    try:
        # Get db_content directory from environment variable
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        db_path = os.path.join(db_content_dir, 'media_items.db')
        
        if not os.path.exists(db_path):
            logging.warning("media_items.db not found, no backup created.")
            return
            
        # Create backup directory if it doesn't exist
        backup_dir = os.path.join(db_content_dir, 'backups')
        os.makedirs(backup_dir, exist_ok=True)
        
        # Generate backup filename with timestamp
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = os.path.join(backup_dir, f'media_items_{timestamp}.db')
        
        # Create the backup
        shutil.copy2(db_path, backup_path)
        logging.info(f"Backup of media_items.db created: {backup_path}")
        
        # Get list of existing backups and sort by modification time
        existing_backups = [os.path.join(backup_dir, f) for f in os.listdir(backup_dir) 
                          if f.startswith('media_items_') and f.endswith('.db')]
        existing_backups.sort(key=lambda x: os.path.getmtime(x), reverse=True)
        
        # Remove older backups, keeping only the two most recent
        for old_backup in existing_backups[2:]:
            os.remove(old_backup)
            logging.info(f"Removed old backup: {old_backup}")
            
    except Exception as e:
        logging.error(f"Error creating database backup: {str(e)}")

def get_version():
    try:
        # Get the application path based on whether we're frozen or not
        if getattr(sys, 'frozen', False):
            application_path = sys._MEIPASS
        else:
            application_path = os.path.dirname(os.path.abspath(__file__))
        
        version_path = os.path.join(application_path, 'version.txt')
        
        with open(version_path, 'r') as version_file:
            # Read the exact contents and only strip whitespace
            version = version_file.readline().strip()
            return version  # Return immediately to avoid any further processing
    except FileNotFoundError:
        logging.error("version.txt not found")
        return "0.0.0"
    except Exception as e:
        logging.error(f"Error reading version: {e}")
        return "0.0.0"

def signal_handler(signum, frame):
    """Handle termination signals gracefully."""
    stop_program(from_signal=True)
    # Exit directly when handling SIGINT
    if signum == signal.SIGINT:
        stop_global_profiling()
        sys.exit(0)

def update_web_ui_state(state):
    try:
        port = int(os.environ.get('CLI_DEBRID_PORT', 5000))
        api.post(f'http://localhost:{port}/api/update_program_state', json={'state': state})
    except api.exceptions.RequestException:
        logging.error("Failed to update web UI state")

def check_metadata_service():
    grpc_url = get_setting('Metadata Battery', 'url')
    battery_port = int(os.environ.get('CLI_DEBRID_BATTERY_PORT', 5001))
    
    # Remove leading "http://" or "https://"
    grpc_url = re.sub(r'^https?://', '', grpc_url)
    
    # Remove any trailing port numbers and slashes
    grpc_url = re.sub(r':\d+/?$', '', grpc_url)
    
    # Append ":50051"
    grpc_url += ':50051'
    
    try:
        channel = grpc.insecure_channel(grpc_url)
        stub = metadata_service_pb2_grpc.MetadataServiceStub(channel)
        # Try to make a simple call to check connectivity
        stub.TMDbToIMDb(metadata_service_pb2.TMDbRequest(tmdb_id="1"), timeout=5)
        logging.info(f"Successfully connected to metadata service at {grpc_url}")
        return grpc_url
    except grpc.RpcError:
        logging.warning(f"Failed to connect to {grpc_url}, falling back to localhost")
        fallback_urls = ['localhost:50051', 'cli_battery_app:50051']
        for url in fallback_urls:
            try:
                channel = grpc.insecure_channel(url)
                stub = metadata_service_pb2_grpc.MetadataServiceStub(channel)
                stub.TMDbToIMDb(metadata_service_pb2.TMDbRequest(tmdb_id="1"), timeout=5)
                logging.info(f"Successfully connected to metadata service at {url}")
                return url
            except grpc.RpcError:
                logging.warning(f"Failed to connect to metadata service at {url}")
        logging.error("Failed to connect to metadata service on all fallback options")
        return None

def package_app():
    try:
        # Determine the path to version.txt and other resources
        version_path = os.path.join(os.path.dirname(__file__), 'version.txt')
        templates_path = os.path.join(os.path.dirname(__file__), 'templates')
        cli_battery_path = os.path.join(os.path.dirname(__file__), 'cli_battery')
        database_path = os.path.join(os.path.dirname(__file__), 'database')
        content_checkers_path = os.path.join(os.path.dirname(__file__), 'content_checkers')
        debrid_path = os.path.join(os.path.dirname(__file__), 'debrid')
        metadata_path = os.path.join(os.path.dirname(__file__), 'metadata')
        queues_path = os.path.join(os.path.dirname(__file__), 'queues')
        routes_path = os.path.join(os.path.dirname(__file__), 'routes')
        scraper_path = os.path.join(os.path.dirname(__file__), 'scraper')
        static_path = os.path.join(os.path.dirname(__file__), 'static')
        utilities_path = os.path.join(os.path.dirname(__file__), 'utilities')
        icon_path = os.path.join(os.path.dirname(__file__), 'static', 'white-icon-32x32.png')
        
        # Get babelfish data directory
        babelfish_data = get_babelfish_data_dir()
        
        # Add the path to tooltip_schema.json
        tooltip_schema_path = os.path.join(os.path.dirname(__file__), 'tooltip_schema.json')

        # Construct the PyInstaller command
        command = [
            "pyinstaller",
            "--onefile",
            #"--windowed",  # Add this option to prevent the console window
            "--icon", icon_path,  # Use your icon for the application
            "--add-data", f"{version_path};.",
            "--add-data", f"{babelfish_data};babelfish/data",
            "--add-data", f"{templates_path};templates",
            "--add-data", f"{cli_battery_path};cli_battery",
            "--add-data", f"{database_path};database",
            "--add-data", f"{content_checkers_path};content_checkers",
            "--add-data", f"{debrid_path};debrid",
            "--add-data", f"{metadata_path};metadata",
            "--add-data", f"{queues_path};queues",
            "--add-data", f"{routes_path};routes",
            "--add-data", f"{scraper_path};scraper",
            "--add-data", f"{static_path};static",
            "--add-data", f"{utilities_path};utilities",
            "--add-data", f"{icon_path};static",
            # Add the tooltip_schema.json file
            "--add-data", f"{tooltip_schema_path};.",
            "--additional-hooks-dir", "hooks",
            "--hidden-import", "database",
            "--hidden-import", "database.core",
            "--hidden-import", "database.collected_items",
            "--hidden-import", "database.blacklist",
            "--hidden-import", "database.schema_management",
            "--hidden-import", "database.poster_management",
            "--hidden-import", "database.statistics",
            "--hidden-import", "database.wanted_items",
            "--hidden-import", "database.database_reading",
            "--hidden-import", "database.database_writing",
            "--hidden-import", ".MetaData",
            "--hidden-import", ".config",
            "--hidden-import", ".main",
            "--hidden-import", "content_checkers.trakt",
            "--hidden-import", "logging_config",
            "--hidden-import", "main",
            "--hidden-import", "metadata.Metadata",
            "main.py"
        ]
        
        # Run the PyInstaller command
        subprocess.run(command, check=True)
        print("App packaged successfully. Executable is in the 'dist' folder.")
    except subprocess.CalledProcessError as e:
        print(f"An error occurred while packaging the app: {e}")

# Function to check if running as a packaged executable
def is_frozen():
    return getattr(sys, 'frozen', False)

# Modify the setup_tray_icon function
def setup_tray_icon():
    # Only proceed if we're on Windows
    if platform.system() != 'Windows':
        logging.info("Tray icon is only supported on Windows")
        return

    logging.info("Starting setup_tray_icon function")
    
    # Check for FFmpeg installation
    def check_ffmpeg():
        try:
            subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=5, check=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            return False

    # Check FFmpeg
    if not check_ffmpeg():
        logging.warning("FFmpeg not found on system. Some video processing features may not work. Please install FFmpeg manually if needed.")
    else:
        logging.info("FFmpeg is already installed")

    import socket
    ip_address = socket.gethostbyname(socket.gethostname())

    # Launch browser after 2 seconds
    def delayed_browser_launch():
        time.sleep(2)  # Wait for 2 seconds
        try:
            port = int(os.environ.get('CLI_DEBRID_PORT', 5000))
            if check_localhost_binding(port):
                webbrowser.open(f'http://localhost:{port}')
                logging.info("Browser launched successfully")
            else:
                logging.error(f"Failed to bind to localhost:{port}")
        except Exception as e:
            logging.error(f"Failed to launch browser: {e}")
    
    # Start browser launch in a separate thread
    browser_thread = threading.Thread(target=delayed_browser_launch)
    browser_thread.daemon = True
    browser_thread.start()
    
    # Import required modules
    try:
        import pystray
        from pystray import MenuItem as item
        from PIL import Image
        import win32gui
        import win32con
        logging.info("Successfully imported pystray, PIL, and Windows modules")
    except ImportError as e:
        logging.error(f"Failed to import required modules: {e}")
        return

    def minimize_to_tray():
        # Find and hide both the main window and console window
        def enum_windows_callback(hwnd, _):
            window_text = win32gui.GetWindowText(hwnd)
            logging.debug(f"Found window: {window_text}")
            if "cli_debrid" in window_text.lower() and window_text.lower().endswith(".exe"):
                logging.info(f"Hiding window: {window_text}")
                win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
        win32gui.EnumWindows(enum_windows_callback, None)

    def restore_from_tray(icon):
        # Show both the main window and console window
        def enum_windows_callback(hwnd, _):
            window_text = win32gui.GetWindowText(hwnd)
            if "cli_debrid" in window_text.lower() and window_text.lower().endswith(".exe"):
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                win32gui.SetForegroundWindow(hwnd)
        win32gui.EnumWindows(enum_windows_callback, None)

    def restore_menu_action(icon, item):
        restore_from_tray(icon)
        
    def hide_to_tray(icon, item):
        minimize_to_tray()

    def on_exit(icon, item):
        logging.info("Exit option selected from system tray")
        icon.stop()
        # Stop all processes
        global program_runner, metadata_process
        print("\nStopping the program...")

        # Stop the main program runner
        if 'program_runner' in globals() and program_runner:
            program_runner.stop()
            print("Main program stopped.")

        # Terminate the metadata battery process
        with metadata_lock:
            if metadata_process and metadata_process.poll() is None:
                print("Stopping metadata battery...")
                metadata_process.terminate()
                try:
                    metadata_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    metadata_process.kill()
                print("Metadata battery stopped.")

        # Find and terminate all related processes
        current_process = psutil.Process()
        children = current_process.children(recursive=True)
        for child in children:
            print(f"Terminating child process: {child.pid}")
            child.terminate()

        # Wait for all child processes to terminate
        _, still_alive = psutil.wait_procs(children, timeout=5)

        # If any processes are still alive, kill them
        for p in still_alive:
            print(f"Force killing process: {p.pid}")
            p.kill()

        print("All processes terminated.")
        # Force kill all cli_debrid processes
        if is_frozen():
            exe_name = os.path.basename(sys.executable)
        else:
            exe_name = "cli_debrid-" + get_version() + ".exe"
        subprocess.run(['taskkill', '/F', '/IM', exe_name], shell=True)

    # Create the menu
    menu = (
        item('Show', restore_menu_action),
        item('Hide', hide_to_tray),
        item('Exit', on_exit),
    )

    # Get the icon path
    if getattr(sys, 'frozen', False):
        application_path = sys._MEIPASS
    else:
        application_path = os.path.dirname(os.path.abspath(__file__))
    
    # List all windows to help with debugging
    def list_windows():
        def callback(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if title:
                    logging.info(f"Visible window: {title}")
        win32gui.EnumWindows(callback, None)
    
    logging.info("Listing all visible windows:")
    list_windows()
    
    icon_path = os.path.join(application_path, 'static', 'white-icon-32x32.png')
    
    # If the icon doesn't exist in the frozen path, try the static directory
    if not os.path.exists(icon_path):
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'white-icon-32x32.png')
    
    logging.info(f"Using icon path: {icon_path}")
    
    try:
        image = Image.open(icon_path)
        import socket
        ip_address = socket.gethostbyname(socket.gethostname())
        icon = pystray.Icon("CLI Debrid", image, f"CLI Debrid\nMain app: localhost:{os.environ.get('CLI_DEBRID_PORT', '5000')}\nBattery: localhost:{os.environ.get('CLI_DEBRID_BATTERY_PORT', '5001')}", menu)
        
        # Set up double-click handler
        icon.on_activate = restore_from_tray
        
        # Minimize the window to tray when the icon is created
        minimize_to_tray()
        
        icon.run()
    except Exception as e:
        logging.error(f"Failed to create or run system tray icon: {e}")
        return

def check_localhost_binding(port=5000):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('127.0.0.1', port))
        sock.close()
        return True
    except socket.error:
        logging.error(f"Failed to bind to localhost:{port}")
        stop_program()
        return False

# Modify the stop_program function
def stop_program(from_signal=False):
    global program_runner, metadata_process
    print("\nStopping the program...")

    # Stop the main program runner
    if 'program_runner' in globals() and program_runner:
        program_runner.stop()
        print("Main program stopped.")

    # Terminate the metadata battery process
    with metadata_lock:
        if metadata_process and metadata_process.poll() is None:
            print("Stopping metadata battery...")
            metadata_process.terminate()
            try:
                metadata_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                metadata_process.kill()
            print("Metadata battery stopped.")

    # Find and terminate all related processes
    try:
        current_process = psutil.Process()
        children = current_process.children(recursive=True)
        for child in children:
            print(f"Terminating child process: {child.pid}")
            child.terminate()

        # Wait for all child processes to terminate
        _, still_alive = psutil.wait_procs(children, timeout=5)

        # If any processes are still alive, kill them
        for p in still_alive:
            print(f"Force killing process: {p.pid}")
            p.kill()

        print("All processes terminated.")
    except Exception as e:
        print(f"Error while terminating processes: {e}")

    # Only send the interrupt signal if not already handling a signal
    if not from_signal:
        os.kill(os.getpid(), signal.SIGINT)

def update_media_locations():
    """
    Startup function that reviews database items and updates their location_on_disk field
    by checking the zurg_all_folder location.
    """
    import os
    from database.core import get_db_connection
    import logging
    from time import time
    import subprocess
    import shlex
    from collections import defaultdict
    from concurrent.futures import ThreadPoolExecutor, as_completed

    BATCH_SIZE = 1000
    MAX_WORKERS = 4  # Number of parallel workers

    def build_file_map(zurg_all_folder):
        """Build a map of filenames to their full paths using find"""
        cmd = f"find {shlex.quote(zurg_all_folder)} -type f"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            logging.error(f"Error running find command: {result.stderr}")
            return {}
        
        # Create a map of filename -> list of full paths
        file_map = defaultdict(list)
        for path in result.stdout.splitlines():
            if path:
                filename = os.path.basename(path)
                file_map[filename].append(path)
        
        logging.info(f"Built file map with {len(file_map)} unique filenames")
        return file_map

    def process_batch(items_batch, file_map, zurg_all_folder):
        """Process a batch of items and return updates"""
        updates = []
        for item_id, filled_by_file, media_type in items_batch:
            if not filled_by_file:
                continue

            # Check if file exists in our map
            if filled_by_file in file_map:
                paths = file_map[filled_by_file]
                if len(paths) == 1:
                    # Single match - use it
                    updates.append((paths[0], item_id))
                else:
                    # Multiple matches - try to find best match
                    # First try exact folder match
                    direct_path = os.path.join(zurg_all_folder, filled_by_file, filled_by_file)
                    if direct_path in paths:
                        updates.append((direct_path, item_id))
                    else:
                        # Just use the first match
                        updates.append((paths[0], item_id))
                        if len(paths) > 1:
                            logging.debug(f"Multiple matches found for {filled_by_file}, using {paths[0]}")
            else:
                logging.error(f"Could not find location for item {item_id} with file {filled_by_file}")
        
        return updates

    zurg_all_folder = get_setting('File Management', 'zurg_all_folder')
    if not zurg_all_folder:
        logging.error("zurg_all_folder not set in settings")
        return

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Build file map first
        start_time = time()
        file_map = build_file_map(zurg_all_folder)
        logging.info(f"Built file map in {time() - start_time:.1f} seconds")

        # Get all media items that need updating
        cursor.execute("""
            SELECT id, filled_by_file, type 
            FROM media_items 
            WHERE filled_by_file IS NOT NULL 
            AND (location_on_disk IS NULL OR location_on_disk = '')
            AND type != 'movie'
        """)
        items = cursor.fetchall()
        total_items = len(items)
        logging.info(f"Found {total_items} items to process")

        # Process items in parallel batches
        start_time = time()
        all_updates = []
        
        # Split items into batches
        batches = [items[i:i + BATCH_SIZE] for i in range(0, len(items), BATCH_SIZE)]
        
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # Submit all batches to the thread pool
            future_to_batch = {
                executor.submit(process_batch, batch, file_map, zurg_all_folder): batch 
                for batch in batches
            }
            
            # Process completed batches
            for future in as_completed(future_to_batch):
                batch = future_to_batch[future]
                try:
                    updates = future.result()
                    if updates:
                        # Execute database updates
                        cursor.executemany(
                            "UPDATE media_items SET location_on_disk = ? WHERE id = ?",
                            updates
                        )
                        conn.commit()
                        processed = len(updates)
                        all_updates.extend(updates)
                        elapsed = time() - start_time
                        items_per_sec = len(all_updates) / elapsed if elapsed > 0 else 0
                        logging.info(f"Progress: {len(all_updates)}/{total_items} items ({items_per_sec:.1f} items/sec)")
                except Exception as e:
                    logging.error(f"Error processing batch: {str(e)}")

        elapsed = time() - start_time
        items_per_sec = total_items / elapsed if elapsed > 0 else 0
        logging.info(f"Finished updating media locations. Processed {total_items} items in {elapsed:.1f} seconds ({items_per_sec:.1f} items/sec)")
        logging.info(f"Successfully updated {len(all_updates)} items")

    except Exception as e:
        logging.error(f"Error updating media locations: {str(e)}")
        conn.rollback()
    finally:
        conn.close()

def open_log_file():
    log_dir = os.environ.get('USER_LOGS', '/user/logs')
    log_file_path = os.path.join(log_dir, 'debug.log')

    if os.path.exists(log_file_path):
        try:
            if platform.system() == 'Windows':
                os.startfile(log_file_path)
            elif platform.system() == 'Darwin':
                subprocess.call(['open', log_file_path])
            else:
                subprocess.call(['xdg-open', log_file_path])
        except Exception as e:
            logging.error(f"Failed to open log file: {e}")
    else:
        logging.error("Log file does not exist.")

def fix_notification_settings():
    """Check and fix notification settings during startup."""
    try:
        from settings import load_config, save_config
        config = load_config()
        needs_update = False

        if 'Notifications' in config and config['Notifications']:
            for notification_id, notification_config in config['Notifications'].items():
                if notification_config is not None:
                    if 'notify_on' not in notification_config or not notification_config['notify_on']:
                        needs_update = True
                        break

        if needs_update:
            logging.info("Found notifications with missing or empty notify_on settings, fixing...")
            port = int(os.environ.get('CLI_DEBRID_PORT', 5000))
            try:
                response = requests.post(f'http://localhost:{port}/notifications/update_defaults')
                if response.status_code == 200:
                    logging.info("Successfully updated notification defaults")
                else:
                    logging.error(f"Failed to update notification defaults: {response.text}")
            except requests.RequestException as e:
                logging.error(f"Error updating notification defaults: {e}")
    except Exception as e:
        logging.error(f"Error checking notification settings: {e}")

def verify_database_health():
    """
    Verifies the health of both media_items.db and cli_battery.db databases.
    If corruption is detected, backs up the corrupted database and creates a new one.
    """
    logging.info("Verifying database health...")
    
    # Get database paths
    db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    media_items_path = os.path.join(db_content_dir, 'media_items.db')
    cli_battery_path = os.path.join(db_content_dir, 'cli_battery.db')
    
    def check_db_health(db_path, db_name):
        if not os.path.exists(db_path):
            logging.warning(f"{db_name} does not exist, will be created during initialization")
            return True
            
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            # Try to perform a simple query
            cursor.execute("SELECT 1")
            cursor.fetchone()
            
            # Verify database integrity
            cursor.execute("PRAGMA integrity_check")
            result = cursor.fetchone()
            
            cursor.close()
            conn.close()
            
            if result[0] != "ok":
                raise sqlite3.DatabaseError(f"Integrity check failed: {result[0]}")
                
            logging.info(f"{db_name} health check passed")
            return True
            
        except sqlite3.DatabaseError as e:
            logging.error(f"{db_name} is corrupted: {str(e)}")
            
            # Create backup of corrupted database
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_path = f"{db_path}.corrupted_{timestamp}"
            try:
                shutil.copy2(db_path, backup_path)
                logging.info(f"Created backup of corrupted {db_name} at {backup_path}")
            except Exception as backup_error:
                logging.error(f"Failed to create backup of corrupted {db_name}: {str(backup_error)}")
            
            # Delete corrupted database
            try:
                os.remove(db_path)
                logging.info(f"Removed corrupted {db_name}")
            except Exception as del_error:
                logging.error(f"Failed to remove corrupted {db_name}: {str(del_error)}")
                return False
            
            return True
        
        except Exception as e:
            logging.error(f"Error checking {db_name} health: {str(e)}")
            return False
    
    # Check both databases
    media_items_ok = check_db_health(media_items_path, "media_items.db")
    cli_battery_ok = check_db_health(cli_battery_path, "cli_battery.db")
    
    if not media_items_ok or not cli_battery_ok:
        logging.error("Database health check failed")
        return False
    
    logging.info("Database health check completed successfully")
    return True

def main():
    global program_runner, metadata_process
    metadata_process = None

    logging.info("Starting the program...")

    setup_directories()
    backup_config()
    backup_database()
    
    # Verify database health before proceeding
    if not verify_database_health():
        logging.error("Database health check failed. Please check the logs and resolve any issues.")
        return False
    
    # Set up notification handlers
    setup_crash_handler()
    register_shutdown_handler()
    register_startup_handler()

    from settings import ensure_settings_file, get_setting, set_setting
    from database import verify_database
    from database.statistics import get_cached_download_stats
    from not_wanted_magnets import validate_not_wanted_entries
    from config_manager import load_config, save_config

    # Batch set deprecated settings
    set_setting('Debug', 'skip_initial_plex_update', False)
    set_setting('Scraping', 'jackett_seeders_only', True)
    set_setting('Scraping', 'enable_upgrading_cleanup', True)
    set_setting('Staleness Threshold', 'staleness_threshold', 7)
    set_setting('Sync Deletions', 'sync_deletions', True)
    set_setting('Debrid Provider', 'provider', 'RealDebrid')
    set_setting('Debug', 'rescrape_missing_files', True)
    set_setting('Debug', 'anime_renaming_using_anidb', True)

    # Add check for Hybrid uncached management setting
    if get_setting('Scraping', 'uncached_content_handling') == 'Hybrid':
        logging.info("Converting 'Hybrid' uncached content handling setting to 'None' with hybrid_mode=True")
        set_setting('Scraping', 'uncached_content_handling', 'None')
        set_setting('Scraping', 'hybrid_mode', True)
    
    # Get current settings to check if defaults need to be applied
    scraping_settings = get_setting('Scraping')
    debug_settings = get_setting('Debug')
    
    if 'disable_adult' not in scraping_settings:
        set_setting('Scraping', 'disable_adult', True)
        logging.info("Setting default disable_adult to True")

    if 'enable_plex_removal_caching' not in debug_settings:
        set_setting('Debug', 'enable_plex_removal_caching', True)
        logging.info("Setting default enable_plex_removal_caching to True")
    
    if 'trakt_early_releases' not in scraping_settings:
        set_setting('Scraping', 'trakt_early_releases', False)
        logging.info("Setting default trakt_early_releases to False")
        
    # Initialize content_source_check_period if it doesn't exist
    if 'content_source_check_period' not in debug_settings:
        set_setting('Debug', 'content_source_check_period', {})
        logging.info("Initializing content_source_check_period as empty dictionary")

    if get_setting('Debug', 'enabled_separate_anime_folders') is True:
        set_setting('Debug', 'enable_separate_anime_folders', True)
        # Remove the old setting key
        config = load_config()
        if 'Debug' in config and 'enabled_separate_anime_folders' in config['Debug']:
            del config['Debug']['enabled_separate_anime_folders']
            save_config(config)
        logging.info("Migrating enable_separate_anime_folders to True and removing old key")

    # Add migration for media_type setting
    config = load_config()

    # Add migration for folder locations
    if 'Debug' in config:
        updated = False
        
        # Define the default folder names
        default_folders = {
            'movies_folder_name': 'Movies',
            'tv_shows_folder_name': 'TV Shows',
            'anime_movies_folder_name': 'Anime Movies',
            'anime_tv_shows_folder_name': 'Anime TV Shows'
        }
        
        # Ensure all folder name keys exist with default values
        for folder_key, default_value in default_folders.items():
            if folder_key not in config['Debug']:
                config['Debug'][folder_key] = default_value
                updated = True
                logging.info(f"Created missing folder key {folder_key} with default value: {default_value}")
        
        # Create a copy of the items to iterate over
        debug_items = list(config['Debug'].items())
        for key, value in debug_items:
            if key.endswith('_folder_name'):
                # Get the base key without _folder_name
                base_key = key.replace('_folder_name', '')
                # Create new key by appending _folder_name
                new_key = base_key + '_folder_name'
                # Keep the existing value if it exists, otherwise use the default
                if new_key in config['Debug']:
                    config['Debug'][new_key] = config['Debug'][new_key]  # Preserve existing value
                else:
                    config['Debug'][new_key] = default_folders.get(new_key, value)
                updated = True
        
        if updated:
            save_config(config)
            logging.info("Successfully migrated folder name settings")

    if 'Content Sources' in config:
        updated = False
        for source_id, source_config in config['Content Sources'].items():
            # Skip the Collected source as it doesn't use media_type
            if source_id.startswith('Collected_'):
                continue
            
            # Check if media_type is missing
            if 'media_type' not in source_config:
                # Create new ordered dict with desired key order
                new_config = {}
                # Copy existing keys except display_name
                for key in source_config:
                    if key != 'display_name':
                        new_config[key] = source_config[key]
                # Add media_type before display_name
                new_config['media_type'] = 'All'
                # Add display_name last if it exists
                if 'display_name' in source_config:
                    new_config['display_name'] = source_config['display_name']
                
                # Replace the old config with the new ordered one
                config['Content Sources'][source_id] = new_config
                logging.info(f"Adding default media_type 'All' to content source {source_id}")
                updated = True
        
        # Save the updated config if changes were made
        if updated:
            save_config(config)
            logging.info("Successfully migrated content sources to include media_type setting")

    # Add require_physical_release to existing versions
    if 'Scraping' in config and 'versions' in config['Scraping']:
        modified = False
        for version in config['Scraping']['versions']:
            if 'require_physical_release' not in config['Scraping']['versions'][version]:
                config['Scraping']['versions'][version]['require_physical_release'] = False
                modified = True
            # Convert string "true"/"false" to boolean
            elif isinstance(config['Scraping']['versions'][version]['require_physical_release'], str):
                str_value = str(config['Scraping']['versions'][version]['require_physical_release']).lower()
                # Let Python's bool and json.dump handle the casing
                config['Scraping']['versions'][version]['require_physical_release'] = str_value in ('true', 'True', 'TRUE')
                modified = True
                logging.info(f"Converting string '{str_value}' to boolean for require_physical_release in version {version}")
        
        if modified:
            save_config(config)
            logging.info("Added/fixed require_physical_release setting in existing versions")

    # Add migration for notification settings
    if 'Notifications' in config:
        notifications_updated = False
        default_notify_on = {
            'collected': True,
            'wanted': False,
            'scraping': False,
            'adding': False,
            'checking': False,
            'sleeping': False,
            'unreleased': False,
            'blacklisted': False,
            'pending_uncached': False,
            'upgrading': False,
            'program_stop': True,
            'program_crash': True,
            'program_start': True,
            'program_pause': True,
            'program_resume': True,
            'queue_pause': True,
            'queue_resume': True,
            'queue_start': True,
            'queue_stop': True
        }

        for notification_id, notification_config in config['Notifications'].items():
            if notification_config is not None:
                # Check if notify_on is missing or incomplete
                if 'notify_on' not in notification_config or not isinstance(notification_config['notify_on'], dict):
                    notification_config['notify_on'] = default_notify_on.copy()
                    notifications_updated = True
                    logging.info(f"Adding default notify_on settings to notification {notification_id}")
                else:
                    # Add any missing notification types
                    for key, default_value in default_notify_on.items():
                        if key not in notification_config['notify_on']:
                            notification_config['notify_on'][key] = default_value
                            notifications_updated = True
                            logging.info(f"Adding missing notify_on setting '{key}' to notification {notification_id}")

        # Save the updated config if changes were made
        if notifications_updated:
            save_config(config)
            logging.info("Successfully migrated notifications to include all notify_on settings")

    # Add migration for version wake_count setting
    if 'Scraping' in config and 'versions' in config['Scraping']:
        versions_updated = False
        for version_name, version_config in config['Scraping']['versions'].items():
            # Check if wake_count is missing or is string "None"
            if 'wake_count' not in version_config or version_config['wake_count'] == "None":
                version_config['wake_count'] = None  # Set to actual None value
                versions_updated = True
                logging.info(f"Adding/fixing wake_count setting to version {version_name}")
            # Also convert string "None" to actual None if it exists
            elif isinstance(version_config['wake_count'], str) and version_config['wake_count'].lower() == "none":
                version_config['wake_count'] = None
                versions_updated = True
                logging.info(f"Converting string 'None' to actual None for version {version_name}")

        # Save the updated config if changes were made
        if versions_updated:
            save_config(config)
            logging.info("Successfully migrated version settings to include wake_count")

    # Add migration for bitrate filter settings in versions
    if 'Scraping' in config and 'versions' in config['Scraping']:
        versions_updated = False
        for version_name, version_config in config['Scraping']['versions'].items():
            # Add min_bitrate_mbps if missing
            if 'min_bitrate_mbps' not in version_config:
                version_config['min_bitrate_mbps'] = 0.01
                versions_updated = True
                logging.info(f"Adding min_bitrate_mbps setting to version {version_name}")
            
            # Add max_bitrate_mbps if missing
            if 'max_bitrate_mbps' not in version_config:
                version_config['max_bitrate_mbps'] = float('inf')
                versions_updated = True
                logging.info(f"Adding max_bitrate_mbps setting to version {version_name}")
            
            # Convert string values to float if needed
            if isinstance(version_config.get('min_bitrate_mbps'), str):
                try:
                    # Handle blank/empty min_bitrate
                    if not version_config['min_bitrate_mbps'].strip():
                        version_config['min_bitrate_mbps'] = 0.01
                    else:
                        version_config['min_bitrate_mbps'] = float(version_config['min_bitrate_mbps'])
                    versions_updated = True
                    logging.info(f"Converting min_bitrate_mbps to float for version {version_name}")
                except ValueError:
                    version_config['min_bitrate_mbps'] = 0.01
                    versions_updated = True
                    logging.warning(f"Invalid min_bitrate_mbps value in version {version_name}, resetting to 0.01")
            
            if isinstance(version_config.get('max_bitrate_mbps'), str):
                try:
                    # Handle blank/empty max_bitrate
                    if not version_config['max_bitrate_mbps'].strip():
                        version_config['max_bitrate_mbps'] = float('inf')
                    elif version_config['max_bitrate_mbps'].lower() in ('inf', 'infinity'):
                        version_config['max_bitrate_mbps'] = float('inf')
                    else:
                        version_config['max_bitrate_mbps'] = float(version_config['max_bitrate_mbps'])
                    versions_updated = True
                    logging.info(f"Converting max_bitrate_mbps to float for version {version_name}")
                except ValueError:
                    version_config['max_bitrate_mbps'] = float('inf')
                    versions_updated = True
                    logging.warning(f"Invalid max_bitrate_mbps value in version {version_name}, resetting to infinity")

        # Save the updated config if changes were made
        if versions_updated:
            save_config(config)
            logging.info("Successfully migrated version settings to include bitrate filters")

    # Check and set upgrading_percentage_threshold if blank
    threshold_value = get_setting('Scraping', 'upgrading_percentage_threshold', '0.1')
    if not str(threshold_value).strip():
        set_setting('Scraping', 'upgrading_percentage_threshold', '0.1')
        logging.info("Set blank upgrading_percentage_threshold to default value of 0.1")

    # Get battery port from environment variable
    battery_port = int(os.environ.get('CLI_DEBRID_BATTERY_PORT', '5001'))
    
    # Set metadata battery URL with the correct port
    set_setting('Metadata Battery', 'url', f'http://localhost:{battery_port}')
    #logging.info(f"Set metadata battery URL to http://localhost:{battery_port}")

    ensure_settings_file()
    verify_database()
    validate_not_wanted_entries()

    # Initialize download stats cache
    try:
        #logging.info("Initializing download stats cache...")
        get_cached_download_stats()
        #logging.info("Download stats cache initialized successfully")
    except Exception as e:
        logging.error(f"Error initializing download stats cache: {str(e)}")

    # Add delay to ensure server is ready
    time.sleep(2)

    # Fix notification settings if needed
    fix_notification_settings()

    # Validate Plex tokens on startup
    token_status = validate_plex_tokens()
    for username, status in token_status.items():
        if not status['valid']:
            logging.error(f"Invalid Plex token for user {username}")

    # Add the update_media_locations call here
    # update_media_locations()

    os.system('cls' if os.name == 'nt' else 'clear')

    version = get_version()

    # Display logo and web UI message
    import socket
    ip_address = socket.gethostbyname(socket.gethostname())
    print(r"""
      (            (             )           (     
      )\ (         )\ )   (   ( /(  (   (    )\ )  
  (  ((_))\       (()/(  ))\  )\()) )(  )\  (()/(  
  )\  _ ((_)       ((_))/((_)((_)\ (()\((_)  ((_)) 
 ((_)| | (_)       _| |(_))  | |(_) ((_)(_)  _| |  
/ _| | | | |     / _` |/ -_) | '_ \| '_|| |/ _` |  
\__| |_| |_|_____\__,_|\___| |_.__/|_|  |_|\__,_|  
           |_____|                                 

           Version:                      
    """)
    print(f"             {version}\n") 
    print(f"cli_debrid is initialized.")
    port = int(os.environ.get('CLI_DEBRID_PORT', 5000))
    print(f"The web UI is available at http://localhost:{port}")
    print("Use the web UI to control the program.")
    print("Press Ctrl+C to stop the program.")

    # Start the system tray icon if running as a packaged Windows app
    if is_frozen() and platform.system() == 'Windows':
        # Import Windows-specific modules only on Windows
        import win32gui
        import win32con
        # Start the system tray icon
        tray_thread = threading.Thread(target=setup_tray_icon)
        tray_thread.daemon = True
        tray_thread.start()

    # Run the metadata battery only on Windows
    is_windows = platform.system() == 'Windows'
    if is_windows:
        # Start the metadata battery
        print("Running on Windows. Starting metadata battery...")
    else:
        print("Running on a non-Windows system. Metadata battery will not be started.")

    # Always print this message
    print("Running in console mode.")

    if get_setting('Debug', 'auto_run_program'):
        # Add delay to ensure server is ready
        time.sleep(2)  # Wait for server to initialize
        # Call the start_program route
        try:
            port = int(os.environ.get('CLI_DEBRID_PORT', 5000))
            response = requests.post(f'http://localhost:{port}/program_operation/api/start_program')
            if response.status_code == 200:
                print("Program started successfully")
            else:
                print(f"Failed to start program. Status code: {response.status_code}")
                print(f"Response: {response.text}")
        except requests.RequestException as e:
            print(f"Error calling start_program route: {e}")

    # Set up signal handling
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Main loop
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        from program_operation_routes import cleanup_port
        cleanup_port()
        stop_program()
        stop_global_profiling()
        print("Program stopped.")

def package_main():
    setup_logging()
    package_app()

def print_version():
    try:
        with open('version.txt', 'r') as f:
            version = f.read().strip()
            print(f"Version:\n\n\t     {version}\n")
    except Exception as e:
        logging.error(f"Failed to read version: {e}")
        print("Version: Unknown\n")

if __name__ == "__main__":
    try:
        # Choose whether to run the normal app or package it
        if len(sys.argv) > 1 and sys.argv[1] == "--package":
            package_main()
        else:
            setup_logging()
            start_global_profiling()
            
            from api_tracker import setup_api_logging
            setup_api_logging()
            from web_server import start_server
            
            print_version()
            print("\ncli_debrid is initialized.")
            
            def run_flask():
                if not start_server():
                    return False
                return True

            if not run_flask():
                stop_program()
                sys.exit(1)
                
            print("The web UI is available at http://localhost:5000")
            main()
    except KeyboardInterrupt:
        stop_global_profiling()
        print("Program stopped.")