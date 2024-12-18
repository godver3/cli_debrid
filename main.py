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
    for log_file in ['debug.log', 'info.log', 'queue.log']:
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

def get_version():
    try:
        # Get the application path based on whether we're frozen or not
        if getattr(sys, 'frozen', False):
            application_path = sys._MEIPASS
        else:
            application_path = os.path.dirname(os.path.abspath(__file__))
        
        version_path = os.path.join(application_path, 'version.txt')
        logging.info(f"Reading version from: {version_path}")
        
        with open(version_path, 'r') as version_file:
            version = version_file.read().strip()
    except FileNotFoundError:
        logging.error("version.txt not found")
        version = "0.0.0"
    except Exception as e:
        logging.error(f"Error reading version: {e}")
        version = "0.0.0"
    return version

def signal_handler(signum, frame):
    stop_program(from_signal=True)
    # Exit directly when handling SIGINT
    if signum == signal.SIGINT:
        stop_global_profiling()
        sys.exit(0)

def update_web_ui_state(state):
    try:
        api.post('http://localhost:5000/api/update_program_state', json={'state': state})
    except api.exceptions.RequestException:
        logging.error("Failed to update web UI state")

def check_metadata_service():
    grpc_url = get_setting('Metadata Battery', 'url')
    
    # Remove leading "http://" or "https://"
    grpc_url = re.sub(r'^https?://', '', grpc_url)
    
    # Remove trailing ":5000" or ":5000/" if present
    grpc_url = grpc_url.rstrip('/').removesuffix(':5001')
    grpc_url = grpc_url.rstrip('/').removesuffix(':50051')
    
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

    def install_ffmpeg():
        try:
            if platform.system() != 'Windows':
                logging.warning("FFmpeg automatic installation is only supported on Windows")
                return False
                
            logging.info("Attempting to install FFmpeg using winget...")
            # Check if winget is available first
            try:
                subprocess.run(['winget', '--version'], capture_output=True, timeout=5, check=True)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                logging.info("Winget not available or not responding, attempting manual FFmpeg installation...")
                try:
                    import requests
                    import zipfile
                    import winreg
                    
                    # Create FFmpeg directory in AppData
                    appdata = os.path.join(os.environ['LOCALAPPDATA'], 'FFmpeg')
                    os.makedirs(appdata, exist_ok=True)
                    
                    # Download FFmpeg
                    url = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
                    logging.info("Downloading FFmpeg...")
                    response = requests.get(url, stream=True, timeout=30)
                    zip_path = os.path.join(appdata, 'ffmpeg.zip')
                    with open(zip_path, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            f.write(chunk)
                    
                    # Extract FFmpeg
                    logging.info("Extracting FFmpeg...")
                    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                        zip_ref.extractall(appdata)
                    
                    # Find the bin directory in extracted contents
                    extracted_dir = next(d for d in os.listdir(appdata) if d.startswith('ffmpeg-'))
                    bin_path = os.path.join(appdata, extracted_dir, 'bin')
                    
                    # Add to PATH
                    logging.info("Adding FFmpeg to PATH...")
                    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment', 0, winreg.KEY_ALL_ACCESS)
                    current_path = winreg.QueryValueEx(key, 'Path')[0]
                    if bin_path not in current_path:
                        new_path = current_path + ';' + bin_path
                        winreg.SetValueEx(key, 'Path', 0, winreg.REG_EXPAND_SZ, new_path)
                        winreg.CloseKey(key)
                        # Notify Windows of environment change
                        import win32gui, win32con
                        win32gui.SendMessage(win32con.HWND_BROADCAST, win32con.WM_SETTINGCHANGE, 0, 'Environment')
                    
                    # Clean up zip file
                    os.remove(zip_path)
                    
                    logging.info("FFmpeg installed successfully")
                    # Update current process environment
                    os.environ['PATH'] = new_path
                    return True
                    
                except Exception as e:
                    logging.error(f"Error during manual FFmpeg installation: {e}")
                    return False
                
            # Install FFmpeg using winget with auto-accept
            try:
                # Install FFmpeg with auto-accept
                logging.info("Installing FFmpeg (this may take a few minutes)...")
                process = subprocess.Popen(
                    ['winget', 'install', '--id', 'Gyan.FFmpeg', '--source', 'winget', '--accept-package-agreements'],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    universal_newlines=True,
                    bufsize=1  # Line buffered
                )
                
                # Print output in real-time
                try:
                    while True:
                        output = process.stdout.readline()
                        if output:
                            output = output.strip()
                            if output:  # Only log non-empty lines
                                logging.info(f"winget: {output}")
                                # If we see the download URL, we know it's starting
                                if "Downloading" in output:
                                    logging.info("Download started - please wait...")
                        error = process.stderr.readline()
                        if error:
                            error = error.strip()
                            if error:  # Only log non-empty lines
                                logging.error(f"winget error: {error}")
                        # If process has finished and no more output, break
                        if output == '' and error == '' and process.poll() is not None:
                            break
                except KeyboardInterrupt:
                    logging.warning("Installation interrupted by user")
                    process.terminate()
                    return False
                
                if process.returncode == 0:
                    logging.info("FFmpeg installed successfully")
                    return True
                else:
                    logging.error(f"Failed to install FFmpeg with winget (exit code: {process.returncode})")
                    # Fallback to manual installation
                    logging.info("Falling back to manual installation...")
                    return install_ffmpeg()  # Recursive call will try manual installation
            except subprocess.TimeoutExpired:
                logging.error("Winget installation timed out, falling back to manual installation...")
                return install_ffmpeg()  # Recursive call will try manual installation
                
        except Exception as e:
            logging.error(f"Error installing FFmpeg: {e}")
            return False

    # Check and install FFmpeg if needed
    if not check_ffmpeg():
        logging.info("FFmpeg not found on system, attempting to install...")
        if not install_ffmpeg():
            logging.warning("Failed to install FFmpeg. Some video processing features may not work.")
        else:
            logging.info("FFmpeg installation completed successfully")
    else:
        logging.info("FFmpeg is already installed")
    
    import socket
    ip_address = socket.gethostbyname(socket.gethostname())

    # Launch browser after 2 seconds
    def delayed_browser_launch():
        time.sleep(2)  # Wait for 2 seconds
        try:
            webbrowser.open(f'http://{ip_address}:5000')
            logging.info("Browser launched successfully")
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
        icon = pystray.Icon("CLI Debrid", image, f"CLI Debrid\nMain app: {ip_address}:5000\nBattery: {ip_address}:5001", menu)
        
        # Set up double-click handler
        icon.on_activate = restore_from_tray
        
        # Minimize the window to tray when the icon is created
        minimize_to_tray()
        
        icon.run()
    except Exception as e:
        logging.error(f"Failed to create or run system tray icon: {e}")
        return

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

# Update the main function to use a single thread for the metadata battery
def main():
    global program_runner, metadata_process
    metadata_process = None

    logging.info("Starting the program...")

    setup_directories()
    backup_config()

    from settings import ensure_settings_file, get_setting, set_setting
    from database import verify_database

    # Add check for Hybrid uncached management setting
    if get_setting('Scraping', 'uncached_content_handling') == 'Hybrid':
        logging.info("Resetting 'Hybrid' uncached content handling setting to None")
        set_setting('Scraping', 'uncached_content_handling', 'None')

    set_setting('Metadata Battery', 'url', 'http://localhost:50051')

    ensure_settings_file()
    verify_database()

    # Add the update_media_locations call here
    # update_media_locations()

    os.system('cls' if os.name == 'nt' else 'clear')

    version = get_version()

    # Display logo and web UI message
    import socket
    ip_address = socket.gethostbyname(socket.gethostname())
    print(f"""
      (            (             )           (     
      )\ (         )\ )   (   ( /(  (   (    )\ )  
  (  ((_))\       (()/(  ))\  )\()) )(  )\  (()/(  
  )\  _ ((_)       _| |(_))  | |(_) ((_)(_)  _| |  
/ _| | | | |     / _` |/ -_) | '_ \| '_|| |/ _` |  
\__| |_| |_|_____\__,_|\___| |_.__/|_|  |_|\__,_|  
           |_____|                                 

           Version: {version}                      
    """)
    print(f"cli_debrid is initialized.")
    print(f"The web UI is available at http://{ip_address}:5000")
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
        # Call the start_program route
        try:
            response = requests.post('http://localhost:5000/program_operation/api/start_program')
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
        stop_program()

def package_main():
    setup_logging()
    package_app()

if __name__ == "__main__":
    setup_logging()
    start_global_profiling()
        
    from api_tracker import setup_api_logging
    setup_api_logging()
    from web_server import start_server
    start_server()
    try:
        # Choose whether to run the normal app or package it
        if len(sys.argv) > 1 and sys.argv[1] == "--package":
            package_main()
        else:
            main()
    except KeyboardInterrupt:
        stop_global_profiling()
        print("Program stopped.")