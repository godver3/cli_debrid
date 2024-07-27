import os
import logging
from questionary import select
from run_program import run_program
from settings import SettingsEditor, get_setting
from utilities.debug_commands import debug_commands
from utilities.manual_scrape import run_manual_scrape
from database import verify_database
import logging_config
from scraper_tester import run_tester

logging_config.setup_logging()

# Ensure logs directory exists
if not os.path.exists('logs'):
    os.makedirs('logs')

def main_menu():
    logging.debug("Main menu started")
    logging.debug("Debug logging started")
    os.system('clear')
    while True:
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

def main():
    verify_database()
    
    # Display all settings
    # display_settings()
    
    # Check for the debug flag
    skip_menu = get_setting('Debug', 'skip_menu', default=False)
    
    if skip_menu:
        logging.debug("Debug flag 'skip_menu' is set. Skipping menu and running program directly.")
        run_program()
    else:
        main_menu()

if __name__ == "__main__":
    main()
