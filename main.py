import os
import logging
import requests
import configparser
import inspect
from settings import SettingsEditor, get_setting, load_config, save_config, CONFIG_FILE, ensure_settings_file

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

# Ensure settings file exists and populate with default keys
ensure_settings_file()

from questionary import select
from run_program import run_program
from utilities.debug_commands import debug_commands
from utilities.manual_scrape import run_manual_scrape
from database import verify_database
import logging_config
from scraper_tester import run_tester

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
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    realdebrid_api_key = get_setting('RealDebrid', 'api_key')
    torrentio_enabled = get_setting('Torrentio', 'enabled', 'False')
    knightcrawler_enabled = get_setting('Knightcrawler', 'enabled', 'False')
    comet_enabled = get_setting('Comet', 'enabled', 'False')

    if not plex_url or not plex_token:
        errors.append("Plex URL or token is missing.")
    if not overseerr_url or not overseerr_api_key:
        errors.append("Overseerr URL or API key is missing.")
    if not realdebrid_api_key:
        errors.append("Real-Debrid API key is missing.")
    if not (torrentio_enabled or knightcrawler_enabled or comet_enabled):
        errors.append("At least one scraper (Torrentio, Knightcrawler, Comet) must be enabled.")

    try:
        if plex_url and plex_token:
            response = requests.get(plex_url, headers={'X-Plex-Token': plex_token})
            if response.status_code != 200:
                errors.append("Plex URL or token is not reachable.")
    except Exception as e:
        errors.append(f"Plex URL or token validation failed: {str(e)}")

    try:
        if overseerr_url and overseerr_api_key:
            response = requests.get(overseerr_url, headers={'X-Api-Key': overseerr_api_key})
            if response.status_code != 200:
                errors.append("Overseerr URL or API key is not reachable.")
    except Exception as e:
        errors.append(f"Overseerr URL or API key validation failed: {str(e)}")

    return errors

def main_menu():
    logging.debug("Main menu started")
    logging.debug("Debug logging started")
    os.system('clear')

    version = get_version()
    while True:
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
        action = select(
            "Select an action:",
            choices=[
                "Run Program",
                "Edit Settings",
                "Manual Scrape",
                "Scraper Tester",
                "Debug Commands",
                "Exit"
            ]
        ).ask()

        os.system('clear')

        if action == "Run Program":
            errors = check_required_settings()
            if errors:
                for error in errors:
                    logging.error(error)
                print("Launch failed due to the following errors:")
                for error in errors:
                    print(f"- {error}")
            else:
                run_program()
        elif action == "Edit Settings":
            SettingsEditor()
        elif action == "Manual Scrape":
            run_manual_scrape()
        elif action == "Scraper Tester":
            run_tester()
        elif action == "Debug Commands":
            debug_commands()
        elif action == "Exit":
            logging.debug("Exiting program.")
            break
        logging.debug("Returned to main menu.")

def wait_for_valid_key():
    ignored_keys = {'export LANG=C.UTF-8', 'export LC_ALL=C.UTF-8', 'clear'}
    while True:
        key_press = input().strip()
        if key_press in ignored_keys:
            os.system('clear')
            print("Press any key for Main Menu...")
        else:
            break

def main():
    verify_database()
    os.system('clear')
    # Display all settings
    # display_settings()
    
    # Check for the debug flag
    skip_menu = get_setting('Debug', 'skip_menu', default=False)
    
    if skip_menu:
        logging.debug("Debug flag 'skip_menu' is set. Skipping menu and running program directly.")
        run_program()
    else:
        print("Press any key for Main Menu...")
        wait_for_valid_key()  # Waits for a valid key press
        main_menu()

if __name__ == "__main__":
    main()
