import os
import sys
import threading
import traceback

# Add the project root and cli_battery directories to the Python path
project_root = os.path.dirname(os.path.abspath(__file__))
cli_battery_path = os.path.join(project_root, 'cli_battery')
sys.path.append(project_root)
sys.path.append(cli_battery_path)

try:
    import requests
    print("Requests module imported successfully")
except ImportError as e:
    print(f"Error importing requests: {e}")
    print("Traceback:")
    traceback.print_exc()

# Import main app and battery main
import main as main_app
from cli_battery import main as battery_main

# Import other necessary modules
from database import *
from settings import *
from logging_config import *
from api_tracker import *
from web_server import start_server

def setup_paths():
    if getattr(sys, 'frozen', False):
        base_path = os.path.dirname(sys.executable)
    else:
        base_path = project_root

    os.environ['USER_LOGS'] = os.path.join(base_path, 'user', 'logs')
    os.environ['USER_DB_CONTENT'] = os.path.join(base_path, 'user', 'db_content')
    os.environ['USER_CONFIG'] = os.path.join(base_path, 'user', 'config')

    for path in [os.environ['USER_DB_CONTENT'], os.environ['USER_CONFIG'], os.environ['USER_LOGS']]:
        os.makedirs(path, exist_ok=True)

    for log_file in ['debug.log', 'info.log', 'queue.log']:
        open(os.path.join(os.environ['USER_LOGS'], log_file), 'a').close()

def run_main_app():
    main_app.main()

def run_battery_app():
    try:
        battery_main.main()
    except ImportError as e:
        print(f"Error importing battery main module: {e}")
        print("Traceback:")
        traceback.print_exc()

def create_config_files():
    config_dir = os.environ['USER_CONFIG']
    settings_file = os.path.join(config_dir, 'settings.json')
    pytrakt_file = os.path.join(config_dir, '.pytrakt.json')

    for file in [settings_file, pytrakt_file]:
        if not os.path.exists(file):
            with open(file, 'w') as f:
                f.write('{}')

if __name__ == "__main__":
    setup_paths()
    create_config_files()
    setup_logging()
    start_global_profiling()
    setup_api_logging()
    start_server()

    main_thread = threading.Thread(target=run_main_app)
    battery_thread = threading.Thread(target=run_battery_app)

    main_thread.start()
    battery_thread.start()

    main_thread.join()
    battery_thread.join()

    stop_global_profiling()
