import os
import logging
from questionary import select
from run_program import run_program
from settings import SettingsEditor, get_setting, load_config, save_config, CONFIG_FILE
from utilities.debug_commands import debug_commands
from utilities.manual_scrape import run_manual_scrape
from database import verify_database
import logging_config
from scraper_tester import run_tester

logging_config.setup_logging()

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

# Ensure settings file exists and populate with default keys dynamically
def ensure_settings_file():
    default_settings = {
        'Plex': {
            'url': '',
            'token': ''
        },
        'Overseerr': {
            'url': '',
            'api_key': ''
        },
        'RealDebrid': {
            'api_key': ''
        },
        'Torrentio': {
            'enabled': 'False'
        },
        'Zilean': {
            'url': '',
            'enabled': 'False'
        },
        'Knightcrawler': {
            'url': '',
            'enabled': 'False'
        },
        'Comet': {
            'url': '',
            'enabled': 'False'
        },
        'MDBList': {
            'api_key': '',
            'urls': ''
        },
        'Trakt': {
            'client_id': '',
            'client_secret': ''
        },
        'TMDB': {
            'api_key': ''
        },
        'Queue': {
            'wake_limit': ''
        },
        'Scraping': {
            'enable_4k': 'False',
            'enable_hdr': 'False',
            'resolution_bonus': '',
            'hdr_bonus': '',
            'similarity_threshold_bonus': '',
            'file_size_bonus': '',
            'bitrate_bonus': '',
            'preferred_filter_in': '',
            'preferred_filter_out': '',
            'filter_in': '',
            'filter_out': '',
            'min_size_gb': ''
        },
        'Debug': {
            'logging_level': 'DEBUG',
            'skip_initial_plex_update': 'False',
            'skip_menu': 'False'
        }
    }

    if not os.path.exists(CONFIG_FILE):
        config = load_config()

        for section, settings in default_settings.items():
            if not config.has_section(section):
                config.add_section(section)
            for key, value in settings.items():
                if not config.has_option(section, key):
                    config.set(section, key, value)
        
        save_config(config)

ensure_settings_file()

def main_menu():
    logging.debug("Main menu started")
    logging.debug("Debug logging started")
    os.system('clear')

    while True:
        print("""
          (            (             )           (     
          )\ (         )\ )   (   ( /(  (   (    )\ )  
      (  ((_))\       (()/(  ))\  )\()) )(  )\  (()/(  
      )\  _ ((_)       ((_))/((_)((_)\ (()\((_)  ((_)) 
     ((_)| | (_)       _| |(_))  | |(_) ((_)(_)  _| |  
    / _| | | | |     / _` |/ -_) | '_ \| '_|| |/ _` |  
    \__| |_| |_|_____\__,_|\___| |_.__/|_|  |_|\__,_|  
               |_____|                                 
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
        if key_press not in ignored_keys:
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
