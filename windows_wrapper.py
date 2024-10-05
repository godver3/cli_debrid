import os
import sys
import threading
import main as main_app
from cli_battery import main as battery_main

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
    battery_main.main()

if __name__ == "__main__":
    setup_paths()
    
    # Create threads for both apps
    main_thread = threading.Thread(target=run_main_app)
    battery_thread = threading.Thread(target=run_battery_app)
    
    # Start both threads
    main_thread.start()
    battery_thread.start()
    
    # Wait for both threads to complete
    main_thread.join()
    battery_thread.join()
