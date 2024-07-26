import os
import logging
import time
import configparser
from questionary import select, prompt
from run_program import run_program
from settings import SettingsEditor, get_setting
from utilities.debug_commands import debug_commands
from utilities.manual_scrape import run_manual_scrape
from database import verify_database, create_database
import logging_config

logging_config.setup_logging()

# Ensure logs directory exists
if not os.path.exists('logs'):
    os.makedirs('logs')

def display_settings():
    config = configparser.ConfigParser()
    config.read('config.ini')
    
    print("Current Settings:")
    print("================")
    
    for section in config.sections():
        print(f"\n[{section}]")
        for key, value in config[section].items():
            print(f"{key} = {value} (type: {type(get_setting(section, key)).__name__})")
            print(f"  get_setting value: {get_setting(section, key)} (type: {type(get_setting(section, key)).__name__})")
    
    # Example usage
    logger = logging.getLogger(__name__)
    logger.debug("This is a debug message")
    logger.info("This is an info message")
    
    queue_logger = logging.getLogger('queue')
    queue_logger.info("This is a queue message")
    
    print("\nPress any key to continue...")
    input()

def main_menu():
    logging.info("Main menu started")
    logging.debug("Debug logging started")
    os.system('clear')
    while True:
        action = select(
            "Select an action:",
            choices=[
                "Run Program",
                "Edit Settings",
                "Manual Scrape",
                "Debug Commands",
                "Exit"
            ]
        ).ask()
        if action == "Run Program":
            run_program()
        elif action == "Edit Settings":
            SettingsEditor()
        elif action == "Manual Scrape":
            run_manual_scrape()
        elif action == "Debug Commands":
            debug_commands()
        elif action == "Exit":
            logging.info("Exiting program.")
            break
        logging.info("Returned to main menu.")

def main():
    verify_database()
    
    # Display all settings
    display_settings()
    
    # Check for the debug flag
    skip_menu = get_setting('Logging', 'skip_menu', default=False)
    logging.info(f"skip_menu setting: {skip_menu}")
    logging.info(f"skip_menu setting is: {skip_menu} (type: {type(skip_menu)})")
    
    if skip_menu:
        logging.info("Debug flag 'skip_menu' is set. Skipping menu and running program directly.")
        run_program()
    else:
        main_menu()

if __name__ == "__main__":
    main()