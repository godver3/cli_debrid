import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
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
import json

# Import Windows-specific modules only on Windows
if platform.system() == 'Windows':
    import win32gui
    import win32con

# Existing imports
import shutil
import requests
import re
import subprocess
from utilities.settings import set_setting
from utilities.settings import get_setting
from logging_config import stop_global_profiling, start_global_profiling
import babelfish
from content_checkers.plex_watchlist import validate_plex_tokens
from routes.notifications import (
    setup_crash_handler, 
    register_shutdown_handler, 
    register_startup_handler,
    send_program_stop_notification
)
from database import schema_management

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
from routes.api_tracker import api
from utilities.settings import get_setting
import requests
import re
from utilities.settings import set_setting
import subprocess
import threading
from logging_config import stop_global_profiling, start_global_profiling
import babelfish

# Global variables
metadata_process = None
metadata_lock = threading.Lock()
global_program_runner_instance = None

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
            # Check if auto browser launch is disabled in settings
            if get_setting('UI Settings', 'disable_auto_browser', False):
                logging.info("Automatic browser launch is disabled in settings")
                return
                
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
            elif "npm" in window_text.lower():
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
            elif "npm" in window_text.lower():
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
        # Access ProgramRunner singleton directly
        from queues.run_program import ProgramRunner 
        program_runner_instance = ProgramRunner() # Get singleton instance
        print("\nStopping the program...")

        # Stop the main program runner using the instance
        if program_runner_instance and program_runner_instance.is_running():
            program_runner_instance.stop()
            print("Main program stopped.")

        # Terminate the metadata battery process
        global metadata_process # Keep metadata_process global for now
        with metadata_lock:
            if metadata_process and metadata_process.poll() is None:
                print("Stopping metadata battery...")
                metadata_process.terminate()
                try:
                    metadata_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    metadata_process.kill()
                print("Metadata battery stopped.")

        # Terminate any running phalanx_db_hyperswarm processes
        try:
            # Find any node/npm processes running phalanx_db_hyperswarm
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    cmdline = proc.cmdline()
                    # Check for both direct node processes and npm start processes
                    is_target = (
                        (any('node' in cmd.lower() for cmd in cmdline) and any('phalanx_db' in cmd.lower() for cmd in cmdline)) or
                        (any('node' in cmd.lower() for cmd in cmdline) and any('--expose-gc' in cmd.lower() for cmd in cmdline))
                    )
                    if is_target:
                        print(f"Stopping process: {proc.pid} - {' '.join(cmdline)}")
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except psutil.TimeoutExpired:
                            print(f"Force killing process: {proc.pid}")
                            proc.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
            print("Phalanx DB service stopped.")
        except Exception as e:
            print(f"Error stopping phalanx_db service: {e}")

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
            # Construct the expected executable name if not frozen (adjust if needed)
            version = get_version() 
            exe_name = f"cli_debrid-{version}.exe" # Or adjust based on actual name
            # Fallback if version reading fails or name is different
            if exe_name == "cli_debrid-0.0.0.exe": 
                exe_name = "cli_debrid.exe" # Common fallback
        
        # Use shell=True carefully on Windows for taskkill
        subprocess.run(['taskkill', '/F', '/IM', exe_name], shell=True, check=False) 

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
    # Access ProgramRunner singleton directly
    from queues.run_program import ProgramRunner
    program_runner_instance = ProgramRunner() # Get singleton instance
    # Keep metadata_process global for now
    global metadata_process 
    print("\nStopping the program...")

    # Stop the main program runner using the instance
    if program_runner_instance and program_runner_instance.is_running():
        program_runner_instance.stop()
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
        from utilities.settings import load_config, save_config
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

def migrate_upgrade_rationale():
    """
    Migrates rationale strings in torrent_additions table from
    'Upgrading from version X to None' to 'Upgrading version X'.
    """
    conn = None
    updated_count = 0
    try:
        from database.core import get_db_connection
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Find entries with the specific rationale pattern
        cursor.execute("""
            SELECT id, rationale 
            FROM torrent_additions 
            WHERE rationale LIKE 'Upgrading from version % to None'
        """)
        
        rows_to_update = []
        for row_id, old_rationale in cursor.fetchall():
            try:
                # Extract the original version part
                # Example: "Upgrading from version 1080p to None"
                # prefix = "Upgrading from version " (23 chars)
                # suffix = " to None" (8 chars)
                version_part = old_rationale[23:-8] 
                
                # Construct the new rationale
                new_rationale = f"Upgrading version {version_part}"
                rows_to_update.append((new_rationale, row_id))
            except Exception as e:
                logging.warning(f"Could not process rationale migration for row ID {row_id}: {e}")

        if rows_to_update:
            logging.info(f"Found {len(rows_to_update)} torrent tracking entries to migrate rationale...")
            cursor.executemany("""
                UPDATE torrent_additions 
                SET rationale = ? 
                WHERE id = ?
            """, rows_to_update)
            conn.commit()
            updated_count = cursor.rowcount
            logging.info(f"Successfully migrated rationale for {updated_count} entries.")
        else:
            logging.info("No torrent tracking entries found needing rationale migration.")

    except sqlite3.Error as e:
        logging.error(f"Database error during rationale migration: {e}")
        if conn:
            conn.rollback()
    except Exception as e:
        logging.error(f"Unexpected error during rationale migration: {e}")
    finally:
        if conn:
            conn.close()

def migrate_task_toggles():
    """
    Ensures task_toggles.json exists and contains the migration version marker.
    If the file is missing or the marker is absent/incorrect, it resets the file.
    """
    MIGRATION_VERSION = "0.6.34"
    VERSION_KEY = "_migration_version"
    
    try:
        import os
        import json
        
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        toggles_file_path = os.path.join(db_content_dir, 'task_toggles.json')
        
        needs_reset = False
        current_data = {}

        if os.path.exists(toggles_file_path):
            try:
                with open(toggles_file_path, 'r') as f:
                    current_data = json.load(f)
                if not isinstance(current_data, dict) or current_data.get(VERSION_KEY) != MIGRATION_VERSION:
                    logging.info(f"Task toggles file found but missing or incorrect version marker ({current_data.get(VERSION_KEY)} != {MIGRATION_VERSION}). Resetting.")
                    needs_reset = True
            except json.JSONDecodeError:
                logging.warning(f"Task toggles file exists but is corrupted. Resetting.")
                needs_reset = True
            except Exception as e:
                 logging.error(f"Error reading task toggles file: {e}. Resetting.")
                 needs_reset = True
        else:
            logging.info(f"Task toggles file not found. Creating with version {MIGRATION_VERSION}.")
            needs_reset = True

        if needs_reset:
            try:
                with open(toggles_file_path, 'w') as f:
                    json.dump({VERSION_KEY: MIGRATION_VERSION}, f, indent=4)
                    # Log SUCCESS only *after* successful dump and *before* exiting 'with'
                    logging.info(f"Successfully reset task_toggles.json with version {MIGRATION_VERSION}.")
            except Exception as e:
                # This log will catch errors during open() or json.dump()
                logging.error(f"Failed to write reset task toggles file: {e}")
        # else:
        #     logging.debug(f"Task toggles file already at migration version {MIGRATION_VERSION}. No reset needed.")

    except Exception as e:
        logging.error(f"Unexpected error during task toggles migration check: {e}")

def main():
    # Remove global program_runner from here as well
    global metadata_process 
    metadata_process = None 

    logging.info("Starting the program...")

    # --- START EDIT: Import flask_app and _execute_start_program here ---
    from routes.extensions import app as flask_app 
    from routes.program_operation_routes import _execute_start_program
    # --- END EDIT ---

    setup_directories()
    backup_config()
    backup_database()
    
    # Delete not wanted files on startup
    try:
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        not_wanted_files = ['not_wanted_magnets.pkl', 'not_wanted_urls.pkl']
        for not_wanted_file in not_wanted_files:
            file_path = os.path.join(db_content_dir, not_wanted_file)
            if os.path.exists(file_path):
                os.remove(file_path)
                logging.info(f"Deleted not wanted file on startup: {file_path}")
    except Exception as e:
        logging.warning(f"Could not delete not wanted files on startup: {str(e)}")
    
    # Purge content source cache files on startup
    # try:
    #     logging.info("Purging content source cache files on startup...")
    #     db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    #     deleted_count = 0
    #     for filename in os.listdir(db_content_dir):
    #         if filename.startswith('content_source_') and filename.endswith('_cache.pkl'):
    #             file_path = os.path.join(db_content_dir, filename)
    #             try:
    #                 os.remove(file_path)
    #                 logging.debug(f"Deleted cache file: {file_path}")
    #                 deleted_count += 1
    #             except OSError as e:
    #                 logging.warning(f"Could not delete cache file {file_path}: {e}")
    #     logging.info(f"Deleted {deleted_count} content source cache files.")
    # except Exception as e:
    #     logging.error(f"Error purging content source cache files: {str(e)}")
    
    # Verify database health before proceeding
    if not verify_database_health():
        logging.error("Database health check failed. Please check the logs and resolve any issues.")
        return
    
    # Run specific data migrations first if they don't depend on full schema being present
    # (Keep these if they are safe before full schema verify/migrate)
    migrate_upgrade_rationale()
    migrate_task_toggles()

    # Ensure the main schema (including notifications table) is verified and migrated
    try:
        logging.info("Running database schema verification and migration...")
        # This call implicitly runs migrate_schema() which creates the notifications table
        schema_management.verify_database() 
        logging.info("Database schema verification and migration completed.")
    except Exception as e:
        logging.critical(f"Database verification/migration failed: {e}", exc_info=True)
        return # Stop if DB setup fails

    # Initialize statistics tables/indexes AFTER main schema is verified
    try:
        from database.statistics import get_cached_download_stats # Moved import local
        from database.statistics import update_statistics_summary # Moved import local
        # create_statistics_indexes() # Already called within verify_database via migrate_schema potentially, or needs specific call
        # schema_management.create_statistics_summary_table() # Already called within verify_database via migrate_schema potentially
        logging.info("Initializing statistics summary...")
        update_statistics_summary()
        logging.info("Statistics summary initialized.")
        # Initialize download stats cache
        logging.info("Initializing download stats cache...")
        get_cached_download_stats()
        logging.info("Download stats cache initialized successfully")
    except Exception as e:
        logging.error(f"Error during statistics summary/cache initialization: {e}")

    # Set up notification handlers NOW THAT DB IS READY
    setup_crash_handler()
    register_shutdown_handler()
    register_startup_handler() # This should now succeed

    from utilities.settings import ensure_settings_file, get_setting, set_setting
    # from database import verify_database # No longer needed here
    from database.not_wanted_magnets import validate_not_wanted_entries
    from queues.config_manager import load_config, save_config

    # Batch set deprecated settings
    set_setting('Debug', 'skip_initial_plex_update', False)
    set_setting('Scraping', 'jackett_seeders_only', True)
    set_setting('Scraping', 'enable_upgrading_cleanup', True)
    set_setting('Sync Deletions', 'sync_deletions', True)
    set_setting('Debrid Provider', 'provider', 'RealDebrid')
    set_setting('Debug', 'rescrape_missing_files', False)
    set_setting('Debug', 'anime_renaming_using_anidb', True)
    set_setting('Debug', 'symlink_organize_by_type', True)

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

    # Migrate Emby settings to Emby/Jellyfin settings
    config = load_config()
    if 'Debug' in config:
        emby_settings_updated = False
        
        # Check for old 'emby_url' setting and migrate to 'emby_jellyfin_url'
        if 'emby_url' in config['Debug']:
            emby_url = config['Debug']['emby_url']
            if 'emby_jellyfin_url' not in config['Debug'] or not config['Debug']['emby_jellyfin_url']:
                config['Debug']['emby_jellyfin_url'] = emby_url
                emby_settings_updated = True
                logging.info(f"Migrating 'emby_url' to 'emby_jellyfin_url': {emby_url}")
            # Remove the old setting
            del config['Debug']['emby_url']
            emby_settings_updated = True
        
        # Check for old 'emby_token' setting and migrate to 'emby_jellyfin_token'
        if 'emby_token' in config['Debug']:
            emby_token = config['Debug']['emby_token']
            if 'emby_jellyfin_token' not in config['Debug'] or not config['Debug']['emby_jellyfin_token']:
                config['Debug']['emby_jellyfin_token'] = emby_token
                emby_settings_updated = True
                logging.info(f"Migrating 'emby_token' to 'emby_jellyfin_token'")
            # Remove the old setting
            del config['Debug']['emby_token']
            emby_settings_updated = True
        
        if emby_settings_updated:
            save_config(config)
            logging.info("Successfully migrated Emby settings to Emby/Jellyfin settings")

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

    # Add migration for notification settings (can run later, doesn't affect DB structure)
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

    # Add migration for language_code in versions
    if 'Scraping' in config and 'versions' in config['Scraping']:
        versions_updated = False
        for version_name, version_config in config['Scraping']['versions'].items():
            if 'language_code' not in version_config:
                version_config['language_code'] = 'en'
                versions_updated = True
                logging.info(f"Adding default language_code 'en' to version {version_name}")

        # Save the updated config if changes were made
        if versions_updated:
            save_config(config)
            logging.info("Successfully migrated version settings to include language_code")

    # --- Add migration for fallback_version in versions ---
    if 'Scraping' in config and 'versions' in config['Scraping']:
        versions_updated = False
        for version_name, version_config in config['Scraping']['versions'].items():
            if 'fallback_version' not in version_config:
                version_config['fallback_version'] = 'None'
                versions_updated = True
                logging.info(f"Adding default fallback_version 'None' to version {version_name}")

        # Save the updated config if changes were made
        if versions_updated:
            save_config(config)
            logging.info("Successfully migrated version settings to include fallback_version")
    # --- End fallback_version migration ---

    # --- Add migration for allow_specials in Content Sources ---
    if 'Content Sources' in config:
        content_sources_updated = False
        for source_id, source_config in config['Content Sources'].items():
            if 'allow_specials' not in source_config:
                source_config['allow_specials'] = False
                content_sources_updated = True
                logging.info(f"Adding default allow_specials=False to content source {source_id}")

        if content_sources_updated:
            save_config(config)
            logging.info("Successfully migrated content sources to include allow_specials setting")
    # --- End allow_specials migration ---

    # --- Add migration for custom_symlink_subfolder in Content Sources ---
    if 'Content Sources' in config:
        content_sources_updated = False
        for source_id, source_config in config['Content Sources'].items():
            if 'custom_symlink_subfolder' not in source_config:
                source_config['custom_symlink_subfolder'] = ''
                content_sources_updated = True
                logging.info(f"Adding default custom_symlink_subfolder='' to content source {source_id}")

        if content_sources_updated:
            save_config(config)
            logging.info("Successfully migrated content sources to include custom_symlink_subfolder setting")
    # --- End custom_symlink_subfolder migration ---

    # --- Add migration for year_match_weight in versions ---
    if 'Scraping' in config and 'versions' in config['Scraping']:
        versions_updated = False
        for version_name, version_config in config['Scraping']['versions'].items():
            if 'year_match_weight' not in version_config:
                version_config['year_match_weight'] = 3 # Default to int
                versions_updated = True
                logging.info(f"Adding default year_match_weight 3 to version {version_name}")
            # Convert to int if it's not already an int
            elif not isinstance(version_config['year_match_weight'], int):
                try:
                    # Round float before converting to int
                    if isinstance(version_config['year_match_weight'], float):
                        version_config['year_match_weight'] = int(round(version_config['year_match_weight']))
                    else: # Handle strings or other types
                        version_config['year_match_weight'] = int(round(float(version_config['year_match_weight'])))
                    versions_updated = True
                    logging.info(f"Converting year_match_weight to int for version {version_name}")
                except (ValueError, TypeError):
                    version_config['year_match_weight'] = 3 # Reset to default int if conversion fails
                    versions_updated = True
                    logging.warning(f"Invalid year_match_weight value in version {version_name}, resetting to 3")

        if versions_updated:
            save_config(config)
            logging.info("Successfully migrated version settings to include year_match_weight")
    # --- End year_match_weight migration ---

    # --- Add migration for anime_filter_mode in versions ---
    if 'Scraping' in config and 'versions' in config['Scraping']:
        versions_updated = False
        for version_name, version_config in config['Scraping']['versions'].items():
            if 'anime_filter_mode' not in version_config:
                version_config['anime_filter_mode'] = 'None'
                versions_updated = True
                logging.info(f"Adding default anime_filter_mode 'None' to version {version_name}")

        if versions_updated:
            save_config(config)
            logging.info("Successfully migrated version settings to include anime_filter_mode")
    # --- End anime_filter_mode migration ---

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
    # verify_database() # No longer needed here
    validate_not_wanted_entries()

    # Initialize download stats cache # Moved earlier
    # ...

    # Initialize statistics summary # Moved earlier
    # ...

    # Add delay to ensure server is ready # Maybe less critical now
    time.sleep(1) # Reduced delay slightly

    # Fix notification settings if needed (can run later)
    fix_notification_settings()

    # Validate Plex tokens on startup
    token_status = validate_plex_tokens()
    for username, status in token_status.items():
        if not status['valid']:
            logging.error(f"Invalid Plex token for user {username}")

    # Add the update_media_locations call here
    # update_media_locations() # Keep commented unless needed at startup

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
        # Add delay to ensure server is ready for app_context usage
        time.sleep(3)  # Increased delay slightly to ensure Flask app is fully up for app_context
        
        # --- START EDIT: Directly call _execute_start_program ---
        # The readiness check via HTTP is less critical if we call the Python function directly,
        # but ensuring flask_app.program_runner is ready is vital.
        # The __main__ block should have set up flask_app.program_runner and its listeners.

        logging.info("Auto-start: Attempting to start program by directly calling _execute_start_program...")
        try:
            # Run the internal start function within the Flask app context
            with flask_app.app_context():
                # Ensure current_app.program_runner (via flask_app.program_runner) is available
                if not getattr(flask_app, 'program_runner', None):
                    logging.error("CRITICAL [main.py auto-start]: flask_app.program_runner not set before calling _execute_start_program. Aborting.")
                    print("Failed to auto-start program: Internal setup error (runner not found on app).")
                elif not getattr(flask_app.program_runner, 'initial_listeners_setup_complete', False):
                    logging.error("CRITICAL [main.py auto-start]: flask_app.program_runner listeners not confirmed setup. Aborting direct start.")
                    print("Failed to auto-start program: Internal setup error (runner listeners not ready).")
                else:
                    start_result = _execute_start_program() # Direct call

                    if start_result.get("status") == "success":
                        print("Program started successfully via auto-start (direct call).")
                        logging.info("Program started successfully via auto-start (direct call).")
                    else:
                        message = start_result.get("message", "Unknown error from _execute_start_program.")
                        print(f"Failed to auto-start program (direct call): {message}")
                        logging.error(f"Failed to auto-start program (direct call): {message}. Details: {start_result.get('failed_services_details')}")
        except Exception as e_direct_call:
            print(f"Critical error during direct call to _execute_start_program for auto-start: {e_direct_call}")
            logging.error(f"Critical error during direct call to _execute_start_program for auto-start: {e_direct_call}", exc_info=True)
        # --- END EDIT ---

    # Set up signal handling
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # --- START EDIT: Supervisor attributes ---
    # Using function attributes for state to keep them local to main() context
    if not hasattr(main, 'last_supervisor_check_time'):
        main.last_supervisor_check_time = 0
    if not hasattr(main, 'supervisor_resume_attempts'):
        main.supervisor_resume_attempts = 0
    SUPERVISOR_CHECK_INTERVAL = 300 # 5 minutes
    SUPERVISOR_MAX_RESUME_ATTEMPTS = 3 
    # --- END EDIT ---

    # Main loop
    try:
        while True:
            time.sleep(5) # Main loop poll interval

            # --- START EDIT: Supervisor Logic ---
            # No 'global' keyword needed here to access the module-level variable
            if global_program_runner_instance and global_program_runner_instance.is_running() and False:
                if global_program_runner_instance.queue_paused and global_program_runner_instance.pause_info.get("error_type"):
                    current_time = time.time()
                    if current_time - main.last_supervisor_check_time > SUPERVISOR_CHECK_INTERVAL:
                        main.last_supervisor_check_time = current_time
                        logging.info("[Supervisor] ProgramRunner is paused. Verifying pause conditions.")
                        
                        pause_error_type = global_program_runner_instance.pause_info.get("error_type")
                        pause_reason_str = global_program_runner_instance.pause_info.get("reason_string", "Unknown reason")
                        should_attempt_resume = False

                        if pause_error_type == "CONNECTION_ERROR":
                            logging.info(f"[Supervisor] Pause due to {pause_error_type}. Re-checking service connectivity...")
                            from routes.program_operation_routes import check_service_connectivity # Import locally
                            connectivity_ok, failed_services = check_service_connectivity()
                            if connectivity_ok:
                                logging.info("[Supervisor] Connectivity restored.")
                                should_attempt_resume = True
                            else:
                                logging.info(f"[Supervisor] Connectivity issues persist: {failed_services}")
                        
                        elif pause_error_type == "DB_HEALTH":
                            logging.info(f"[Supervisor] Pause due to {pause_error_type}. Re-checking database health...")
                            if verify_database_health(): # verify_database_health is in main.py
                                logging.info("[Supervisor] Database health is OK.")
                                should_attempt_resume = True
                            else:
                                logging.info("[Supervisor] Database health issues persist.")

                        elif pause_error_type == "SYSTEM_SCHEDULED":
                            logging.info(f"[Supervisor] Pause due to {pause_error_type}. Re-checking system pause schedule...")
                            if not global_program_runner_instance._is_within_pause_schedule():
                                logging.info("[Supervisor] System pause schedule has ended.")
                                should_attempt_resume = True
                            else:
                                logging.info("[Supervisor] Still within system pause schedule.")
                        
                        # Add other specific checks if ProgramRunner introduces new pause_info error_types

                        if should_attempt_resume:
                            main.supervisor_resume_attempts += 1
                            logging.info(f"[Supervisor] Conditions for pause '{pause_reason_str}' (Type: {pause_error_type}) no longer met. Attempting to resume ProgramRunner (Attempt {main.supervisor_resume_attempts}/{SUPERVISOR_MAX_RESUME_ATTEMPTS}).")
                            try:
                                global_program_runner_instance.resume_queue()
                                # resume_queue() clears pause_info if successful
                                if not global_program_runner_instance.queue_paused:
                                    logging.info("[Supervisor] ProgramRunner resumed successfully by supervisor.")
                                    main.supervisor_resume_attempts = 0 # Reset attempts on success
                                else:
                                    logging.warning(f"[Supervisor] Called resume_queue(), but ProgramRunner is still paused. Current pause reason: {global_program_runner_instance.pause_info.get('reason_string')}")
                                    if main.supervisor_resume_attempts >= SUPERVISOR_MAX_RESUME_ATTEMPTS:
                                        logging.error(f"[Supervisor] Max resume attempts ({SUPERVISOR_MAX_RESUME_ATTEMPTS}) reached. ProgramRunner remains paused. Manual intervention may be required. Last pause reason: {pause_reason_str}")
                                        # Optionally, send a critical notification here
                            except Exception as e_resume:
                                logging.error(f"[Supervisor] Error attempting to resume ProgramRunner: {e_resume}", exc_info=True)
                        else:
                            # Conditions for pause still met, reset resume attempts for this specific pause reason if it changes.
                            # This might be too complex; for now, attempts are general.
                            logging.info(f"[Supervisor] Conditions for pause '{pause_reason_str}' (Type: {pause_error_type}) still appear to be met.")
                            # If pause reason changes, supervisor_resume_attempts should ideally reset.
                            # For simplicity, we're not tracking changes in pause_info string here.
                elif not global_program_runner_instance.queue_paused:
                    # If queue is not paused, reset supervisor attempts.
                    if main.supervisor_resume_attempts > 0:
                        logging.info("[Supervisor] ProgramRunner is no longer paused. Resetting resume attempts.")
                        main.supervisor_resume_attempts = 0
                    main.last_supervisor_check_time = time.time() # Update check time even if not paused to delay next check

            # --- END EDIT: Supervisor Logic ---

    except KeyboardInterrupt:
        from routes.program_operation_routes import cleanup_port
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
        if len(sys.argv) > 1 and sys.argv[1] == "--package":
            package_main()
        else:
            setup_logging()
            start_global_profiling()
            
            from routes.api_tracker import setup_api_logging
            setup_api_logging()
            from routes.web_server import start_server
            from routes.extensions import app as flask_app
            from queues.run_program import ProgramRunner, _setup_scheduler_listeners

            print_version()

            program_runner_instance = ProgramRunner()
            program_runner_instance.initial_listeners_setup_complete = False 
            flask_app.program_runner = program_runner_instance # flask_app is now defined
            global_program_runner_instance = program_runner_instance
            
            try:
                _setup_scheduler_listeners(global_program_runner_instance)
                global_program_runner_instance.initial_listeners_setup_complete = True
                logging.info("Initial scheduler listeners set up successfully in __main__ for flask_app.program_runner.")
            except Exception as e_listeners:
                logging.error(f"Failed to set up initial scheduler listeners in __main__: {e_listeners}", exc_info=True)
            
            print("\ncli_debrid Python environment initialized.")

            def run_flask():
                if not start_server(): 
                    return False
                return True

            if not run_flask():
                logging.critical("Flask server failed to start. Exiting.")
                sys.exit(1)
                
            port = int(os.environ.get('CLI_DEBRID_PORT', 5000))
            print(f"The web UI is available at http://localhost:{port}")
            main() 
    except KeyboardInterrupt:
        stop_global_profiling()
        print("Program stopped by KeyboardInterrupt in __main__.")
    except Exception as e_main_startup:
        logging.critical(f"Unhandled exception during __main__ startup: {e_main_startup}", exc_info=True)
        print(f"Critical startup error: {e_main_startup}")