import sys
import os
import appdirs
import threading
import time
import signal
import logging
import platform
import psutil

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
        with open('version.txt', 'r') as version_file:
            version = version_file.read().strip()
    except FileNotFoundError:
        version = "0.0.0"
    return version

def signal_handler(signum, frame):
    stop_program()

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
        icon_path = os.path.join(os.path.dirname(__file__), 'static', 'favicon.png')
        
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
    logging.info("Starting setup_tray_icon function")
    
    # Check if running in a graphical environment
    if "DISPLAY" not in os.environ:
        logging.info("Running in a non-graphical environment. Skipping system tray setup.")
        return

    # Import pystray only if we're in a graphical environment
    try:
        import pystray
        from pystray import MenuItem as item
        from PIL import Image
        logging.info("Successfully imported pystray and PIL")
    except ImportError as e:
        logging.error(f"Failed to import pystray or PIL: {e}")
        return

    def on_exit(icon, item):
        logging.info("Exit option selected from system tray")
        icon.stop()
        stop_program()

    def view_logs(icon, item):
        logging.info("View Logs option selected from system tray")
        open_log_file()

    # Path to your icon image
    icon_image_path = os.path.join(os.path.dirname(sys.executable if is_frozen() else __file__), 'static', 'favicon.png')
    logging.info(f"Icon image path: {icon_image_path}")

    # Check if icon file exists, otherwise create a placeholder
    if not os.path.exists(icon_image_path):
        logging.warning(f"Icon file not found at {icon_image_path}. Creating placeholder.")
        image = Image.new('RGB', (64, 64), color=(73, 109, 137))
    else:
        logging.info("Loading icon image")
        image = Image.open(icon_image_path)

    # Create the system tray icon
    try:
        icon = pystray.Icon(
            "cli_debrid",
            image,
            "cli_debrid",
            menu=pystray.Menu(
                item('View Logs', view_logs),
                item('Exit', on_exit)
            )
        )
        logging.info("System tray icon created successfully")
    except Exception as e:
        logging.error(f"Failed to create system tray icon: {e}")
        return

    # Run the icon
    logging.info("Starting system tray icon")
    try:
        icon.run()
    except Exception as e:
        logging.error(f"Error running system tray icon: {e}")

    logging.info("setup_tray_icon function completed")

# Modify the stop_program function
def stop_program():
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
    sys.exit(0)

# Function to run the metadata battery
def run_metadata_battery():
    global metadata_process
    
    with metadata_lock:
        if metadata_process and metadata_process.poll() is None:
            logging.info("Metadata battery is already running.")
            return

        try:
            # Determine the base path
            if getattr(sys, 'frozen', False):
                # Running as compiled executable
                base_path = sys._MEIPASS
            else:
                # Running in a normal Python environment
                base_path = os.path.dirname(__file__)

            # Construct the path to cli_battery/main.py
            cli_battery_main_path = os.path.join(base_path, 'cli_battery', 'main.py')

            # Check if the file exists
            if not os.path.exists(cli_battery_main_path):
                logging.error(f"cli_battery main.py not found at {cli_battery_main_path}")
                return

            # Start the metadata battery as a subprocess
            metadata_process = subprocess.Popen(
                [sys.executable, cli_battery_main_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )
            logging.info(f"Metadata battery started with PID: {metadata_process.pid}")

        except Exception as e:
            logging.error(f"Error running metadata battery: {e}")
            metadata_process = None

# Update signal handler
def signal_handler(signum, frame):
    stop_program()

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
        # Start the system tray icon
        tray_thread = threading.Thread(target=setup_tray_icon)
        tray_thread.daemon = True
        tray_thread.start()

    # Run the metadata battery only on Windows
    is_windows = platform.system() == 'Windows'
    if is_windows:
        # Start the metadata battery
        metadata_thread = threading.Thread(target=run_metadata_battery)
        metadata_thread.daemon = True
        metadata_thread.start()
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
            # Only check metadata process on Windows
            if is_windows:
                if metadata_process is None or metadata_process.poll() is not None:
                    logging.warning("Metadata battery process has stopped unexpectedly.")
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