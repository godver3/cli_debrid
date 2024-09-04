import logging
logging.getLogger('selector').setLevel(logging.WARNING)
logging.getLogger('asyncio').setLevel(logging.WARNING)

from web_server import start_server
# Start web server
start_server()

import os
import configparser
import inspect
from settings import SettingsEditor, get_setting, load_config, save_config, CONFIG_FILE, ensure_settings_file, set_setting
from database import verify_database
import shutil
from datetime import datetime
import signal
from run_program import run_program, ProgramRunner
import sys
import time
from flask import Flask, current_app
from api_tracker import api
from web_server import app

program_runner = None

# Ensure logs directory exists
if not os.path.exists('logs'):
    os.makedirs('logs')

# Ensure log files exist
for log_file in ['debug.log', 'info.log', 'queue.log']:
    log_path = os.path.join('logs', log_file)
    if not os.path.exists(log_path):
        with open(log_path, 'w'):
            pass

# Ensure db_content directory exists
if not os.path.exists('db_content'):
    os.makedirs('db_content')

config_path = 'config/config.json'
if os.path.exists(config_path):
    #timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f'config/config_backup.json'
    shutil.copy2(config_path, backup_path)
    logging.info(f"Backup of config.json created: {backup_path}")
else:
    logging.warning("config.json not found, no backup created.")

# Ensure settings file exists and populate with default keys
ensure_settings_file()
verify_database()

import logging_config

logging_config.setup_logging()

def get_version():
    try:
        with open('version.txt', 'r') as version_file:
            version = version_file.read().strip()
    except FileNotFoundError:
        version = "0.0.0"
    return version

def check_required_settings():
    errors = []

    plex_url = get_setting('Plex', 'url')
    plex_token = get_setting('Plex', 'token')
    plex_movies = get_setting('Plex', 'movie_libraries')
    plex_shows = get_setting('Plex', 'shows_libraries')
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    realdebrid_api_key = get_setting('RealDebrid', 'api_key')
    torrentio_enabled = get_setting('Torrentio', 'enabled', False)

    if not plex_url or not plex_token:
        errors.append("Plex URL or token is missing.")
    if not plex_movies or not plex_shows:
        errors.append("No Plex libraries provided.")
    if not overseerr_url or not overseerr_api_key:
        errors.append("Overseerr URL or API key is missing.")
    if not realdebrid_api_key:
        errors.append("Real-Debrid API key is missing.")

    try:
        if plex_url and plex_token:
            response = api.get(plex_url, headers={'X-Plex-Token': plex_token})
            if response.status_code != 200:
                errors.append("Plex URL or token is not reachable.")
    except Exception as e:
        errors.append(f"Plex URL or token validation failed: {str(e)}")

    try:
        if overseerr_url and overseerr_api_key:
            response = api.get(f"{overseerr_url}/api/v1/status", headers={'X-Api-Key': overseerr_api_key})
            if response.status_code != 200:
                errors.append("Overseerr URL or API key is not reachable.")
    except Exception as e:
        errors.append(f"Overseerr URL or API key validation failed: {str(e)}")

    return errors


def signal_handler(signum, frame):
    global program_runner
    if program_runner:
        os.system('clear')
        print("\nStopping the program...")
        program_runner.stop()
        print("Program stopped.")
    else:
        print("\nProgram stopped.")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def update_web_ui_state(state):
    try:
        api.post('http://localhost:5000/api/update_program_state', json={'state': state})
    except api.exceptions.RequestException:
        logging.error("Failed to update web UI state")

def main():
    global program_runner
    # Ensure db_content directory exists
    if not os.path.exists('db_content'):
        os.makedirs('db_content')
        logging.info("Created db_content directory.")

    verify_database()
    os.system('clear')
    
    version = get_version()
    
    # Display logo and web UI message
    import socket
    ip_address = socket.gethostbyname(socket.gethostname())
    print(f"""
      (            (             )           (     
      )\ (         )\ )   (   ( /(  (   (    )\ )  
  (  ((_))\       (()/(  ))\  )\()) )(  )\  (()/(  
  )\  _ ((_)       ((_))/((_)((_)\ (()\((_)  ((_)) 
 ((_)| | (_)       _| |(_))  | |(_) ((_)(_)  _| |  
/ _| | | | |     / _` |/ -_) | '_ \| '_|| |/ _` |  
\__| |_| |_|_____\__,_|\___| |_.__/|_|  |_|\__,_|  
           |_____|                                 

           Version: {version}                      
    """)
    print(f"cli_debrid is initialized.")
    print(f"The web UI is available at http://{ip_address}:5000")
    print("Use the web UI to control the program.")
    print("Press Ctrl+C to stop the program.")

    # Keep the script running
    while True:
        time.sleep(1)

if __name__ == "__main__":
    main()