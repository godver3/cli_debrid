import os
import sys
import threading
import traceback

try:
    import requests
    print("Requests module imported successfully")
except ImportError as e:
    print(f"Error importing requests: {e}")
    print("Traceback:")
    traceback.print_exc()

import main as main_app

# Add cli_battery and database directories to Python path
cli_battery_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cli_battery')
database_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'database')
sys.path.append(cli_battery_path)
sys.path.append(database_path)

# Now import the battery main module and database
from cli_battery import main as battery_main
import database

def setup_paths():
    if getattr(sys, 'frozen', False):
        # Running as compiled executable
        base_path = os.path.dirname(sys.executable)
    else:
        # Running as script
        base_path = os.path.dirname(os.path.abspath(__file__))

    # Set up your Unix-like paths
    os.environ['USER_LOGS'] = os.path.join(base_path, 'user', 'logs')
    os.environ['USER_DB_CONTENT'] = os.path.join(base_path, 'user', 'db_content')
    os.environ['USER_CONFIG'] = os.path.join(base_path, 'user', 'config')

    # Create directories if they don't exist
    os.makedirs(os.environ['USER_DB_CONTENT'], exist_ok=True)
    os.makedirs(os.environ['USER_CONFIG'], exist_ok=True)
    os.makedirs(os.environ['USER_LOGS'], exist_ok=True)

    # Create log files if they don't exist
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

    if not os.path.exists(settings_file):
        with open(settings_file, 'w') as f:
            f.write('{}')
    
    if not os.path.exists(pytrakt_file):
        with open(pytrakt_file, 'w') as f:
            f.write('{}')

if __name__ == "__main__":
    setup_paths()
    create_config_files()
    
    # Create threads for both apps
    main_thread = threading.Thread(target=run_main_app)
    battery_thread = threading.Thread(target=run_battery_app)
    
    # Start both threads
    main_thread.start()
    battery_thread.start()
    
    # Wait for both threads to complete
    main_thread.join()
    battery_thread.join()
